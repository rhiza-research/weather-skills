# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.4.0",
# ]
# ///
"""Blend several forecasts at one or more leads into a weighted mean on a common grid."""

import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import ensure_normalized_longitude, grid_spacing
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_SKILLS = Path(__file__).resolve().parents[2]


def _label(path):
    """windborne_wm6_weekly_totals_init2026-11-02.zarr -> windborne_wm6"""
    name = Path(str(path).rstrip("/")).name.removesuffix(".zarr")
    for marker in ("_weekly", "_daily", "_monthly", "_init"):
        name = name.split(marker)[0]
    return name or str(path)


def _lead_dim(da):
    lat_dim, lon_dim = detect_spatial_dims(da)
    extra = [d for d in da.dims if d not in (lat_dim, lon_dim)]
    if len(extra) > 1:
        raise UsageError(
            f"{da.name!r} has dims {list(da.dims)}; reduce it to one lead axis first "
            "(e.g. summarize-dim --dim number for an ensemble mean)"
        )
    return extra[0] if extra else None


def _grid(da):
    """da's lat/lon values (rounded, to compare grids) and its spacing."""
    lat, lon = (da[d].values for d in detect_spatial_dims(da))
    return (tuple(np.round(lat, 4)), tuple(np.round(lon, 4))), max(
        grid_spacing(lat), grid_spacing(lon)
    )


def _regrid(path, variable, grid_zarr, finer, work, label):
    """One input on grid_zarr's grid: coarsen it if finer, downscale it if coarser."""
    skill, args = (
        ("coarsen", ["--method", "conservative"])
        if finer
        else ("downscale", ["--algorithm", "linear-interpolation"])
    )
    script = _SKILLS / skill / "scripts" / f"{skill}.py"
    if not script.is_file():
        # coarsen and downscale live in weather-skills/weather-skills; the weather-skills
        # plugin installs them beside this skill, a checkout of this repo alone does not.
        raise UsageError(
            f"{label} is on another grid, and regridding it needs the {skill} skill beside "
            f"blend ({script}); install the weather-skills plugin, or copy skills/coarsen and "
            "skills/downscale from https://github.com/weather-skills/weather-skills next to skills/blend"
        )
    out = Path(work) / f"{label}.zarr"
    cmd = ["uv", "run", "--quiet", str(script), "-i", str(path), "-o", str(out)]
    cmd += ["--reference-grid", str(grid_zarr), "-v", variable, *args]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise UsageError(f"{label}: {skill} failed:\n{run.stderr.strip()}")
    return xr.open_zarr(out)[variable].load()


def _weights(text, keys, here):
    """Each lead's weights for the forecasts that reach it, scaled to add to 1.

    text is blend-interface's weights ({"week1": {label: weight}, ...}), inline or a .json
    file; a forecast left out of a lead's weights gets 0. Without text, weights are equal.
    """
    if text is None:
        return {n: np.full(len(h), 1 / len(h)) for n, h in here.items()}
    try:
        given = json.loads(text if text.lstrip().startswith("{") else Path(text).read_text())
    except (OSError, ValueError) as exc:
        raise UsageError(f"--weights: not a JSON file or JSON text ({exc})") from None
    labels = set().union(*here.values())
    out = {}
    for n, key in keys.items():
        ws = given.get(key) if isinstance(given, dict) else None
        if not isinstance(ws, dict):
            raise UsageError(f"--weights needs {{label: weight}} for each of {list(keys.values())}")
        if unknown := sorted(set(ws) - labels):
            raise UsageError(f"--weights {key} names {unknown}; the forecasts are {sorted(labels)}")
        if lacking := [k for k, x in ws.items() if k not in here[n] and x > 0]:
            raise UsageError(f"--weights gives weight to {lacking}, which have no {key}")
        w = np.array([float(ws.get(k, 0)) for k in here[n]])
        if (w < 0).any() or w.sum() <= 0:
            raise UsageError(f"--weights {key} must be 0 or more, with at least one above 0")
        out[n] = w / w.sum()
    return out


@weather_skill(name="blend", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), action="append", required=True)
@weather_skill.argument("--label", action="append")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lead", type=int, nargs="+", default=[1])
@weather_skill.argument("--weights")
@weather_skill.argument("--reference-grid", type=Dataset("spatial"))
@weather_skill.argument("--keep-members", action="store_true")
def blend(ds, label, variable, lead, weights, reference_grid, keep_members, **kwargs):
    """Blend several forecasts at one or more leads into a weighted mean on a common grid."""
    paths = [Path(d.attrs["weather_skills_input_path"]) for d in ds]
    labels = label or [_label(p) for p in paths]
    if len(ds) < 2 or len(labels) != len(ds) or len(set(labels)) != len(labels):
        raise UsageError(f"pass two or more --input, each with its own --label (got {labels})")
    ds = [dequantify_dataset(ensure_normalized_longitude(d)) for d in ds]
    if variable is None:
        shared = set.intersection(*(set(d.data_vars) for d in ds))
        if len(shared) != 1:
            raise UsageError(f"inputs share {sorted(shared) or 'no'} variables; pick one with -v")
        variable = shared.pop()
    if missing := [lab for lab, d in zip(labels, ds, strict=True) if variable not in d]:
        raise UsageError(f"no variable {variable!r} in {missing}")
    das = [d[variable] for d in ds]
    dims = [_lead_dim(da) for da in das]

    # Leads are named week1, week2, ... for weekly inputs, else lead1, lead2, ...
    leads = sorted(set(lead))
    word = "week" if das[0].attrs.get("aggregation_period") == "7 day" else "lead"
    keys = {n: f"{word}{n}" for n in leads}
    here = {
        n: [
            lab
            for lab, da, dim in zip(labels, das, dims, strict=True)
            if (n == 1 if dim is None else 1 <= n <= da.sizes[dim])
        ]
        for n in leads
    }
    absent = {n: [lab for lab in labels if lab not in here[n]] for n in leads}
    for n in leads:
        if not here[n]:
            raise UsageError(f"no input reaches {word} {n}")
        if absent[n]:
            print(f"blend: {word} {n} missing from {', '.join(absent[n])}", file=sys.stderr)
    w = _weights(weights, keys, here)
    used = [k for k, lab in enumerate(labels) if any(lab in h for h in here.values())]

    # Target grid: --reference-grid, else the grid most inputs share (ties: the earliest).
    grids = {k: _grid(das[k]) for k in used}
    if reference_grid is not None:
        target = _grid(reference_grid)
    else:
        best = Counter(g for g, _ in grids.values()).most_common(1)[0][0]
        target = next(grids[k] for k in used if grids[k][0] == best)
    (lat_t, lon_t), res_t = target

    # Every forecast on the target grid, as (lead, latitude, longitude).
    fields = {}
    with tempfile.TemporaryDirectory(prefix="blend-") as work:
        grid_zarr = Path(work) / "grid.zarr"
        xr.Dataset(coords={"latitude": list(lat_t), "longitude": list(lon_t)}).assign(
            grid=(("latitude", "longitude"), np.zeros((len(lat_t), len(lon_t)), "float32"))
        ).to_zarr(grid_zarr, consolidated=True)
        for k in used:
            da = das[k]
            if grids[k][0] != (lat_t, lon_t):
                finer = grids[k][1] < res_t * 0.99
                da = _regrid(paths[k], variable, grid_zarr, finer, work, labels[k])
            lat_dim, lon_dim = detect_spatial_dims(da)
            da = da.rename({lat_dim: "latitude", lon_dim: "longitude"})
            # Regridding keeps only the input's extent; pad it back out to the full grid.
            da = da.reindex(
                latitude=list(lat_t), longitude=list(lon_t), method="nearest", tolerance=1e-3
            )
            fields[labels[k]] = da.load()

    def at(lab, n):
        f, dim = fields[lab], dims[labels.index(lab)]
        f = f if dim is None else f.isel({dim: n - 1})
        return f.drop_vars([c for c in f.coords if c not in ("latitude", "longitude")])

    names = [labels[k] for k in used]
    blends, members, weight_map = [], [], {}
    for n in leads:
        stack = xr.concat([at(lab, n) for lab in here[n]], dim=xr.DataArray(here[n], dims="model"))
        weight = xr.DataArray(w[n], dims="model", coords={"model": here[n]})
        # NaN wherever a forecast with weight has no value, so the blend never quietly
        # becomes a mean of fewer forecasts.
        covered = stack.sel(model=weight["model"][weight > 0]).notnull().all("model")
        blends.append((stack.fillna(0) * weight).sum("model").where(covered))
        members.append(stack.reindex(model=names))
        weight_map[keys[n]] = {
            lab: round(float(weight.sel(model=lab)), 4) if lab in here[n] else 0.0 for lab in names
        }

    # The lead axis holds the inputs' own values there (such as each week's start date),
    # from the longest input with a lead axis.
    lead_dim = next((dims[k] for k in used if dims[k] is not None), "lead")
    longest = max(
        (das[k] for k in used if dims[k] == lead_dim),
        key=lambda da: da.sizes[lead_dim],
        default=None,
    )
    attrs = {k: v for k, v in das[0].attrs.items() if not k.startswith("GRIB_")}
    out = xr.Dataset(
        {
            variable: xr.concat(blends, dim=lead_dim).assign_attrs(
                attrs,
                long_name=f"{attrs.get('long_name') or variable} blend",
                blend_weights=json.dumps(weight_map),
            )
        }
    )
    if keep_members:
        out[f"{variable}_members"] = xr.concat(members, dim=lead_dim).assign_attrs(attrs)
    if longest is not None:
        out = out.assign_coords({lead_dim: longest[lead_dim].values[[n - 1 for n in leads]]})
        out[lead_dim].attrs = longest[lead_dim].attrs

    # Stated in full: otherwise the first input's title, source and licence pass through.
    licences = dict.fromkeys(str(d.attrs["license"]) for d in ds if d.attrs.get("license"))
    out.attrs = {
        "title": f"Blend of {len(names)} forecasts, {word}s {' '.join(map(str, leads))}",
        "source": ", ".join(names),
        "weather_skills_source": "blend:" + ",".join(names),
        "license": " | ".join(licences) or "see the inputs",
        "weather_skills_forecast_init": ds[0].attrs.get("weather_skills_forecast_init", ""),
        "blend_leads": json.dumps(leads),
        "blend_weights": json.dumps(weight_map),
        "blend_left_out": json.dumps({keys[n]: absent[n] for n in leads}),
    }
    print(f"blend: weights {json.dumps(weight_map)}", file=sys.stderr)
    return out


if __name__ == "__main__":
    blend()

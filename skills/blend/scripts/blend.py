# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.4.0",
# ]
# ///
"""Blend several forecasts at one or more leads into a weighted mean on a common grid."""

import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import ensure_normalized_longitude, grid_spacing
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_SKILLS = Path(__file__).resolve().parents[2]
_INPUT_PATH_ATTR = "weather_skills_input_path"


def _default_label(path):
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


def _grid_key(da):
    import numpy as np

    lat_dim, lon_dim = detect_spatial_dims(da)
    return tuple(np.round(da[lat_dim].values, 4)), tuple(np.round(da[lon_dim].values, 4))


def _spacing(da):
    lat_dim, lon_dim = detect_spatial_dims(da)
    return max(grid_spacing(da[lat_dim].values), grid_spacing(da[lon_dim].values))


def _write_grid(target, workdir):
    """The target lat/lon as a small Zarr for coarsen / downscale --reference-grid.

    Written fresh because those skills open the reference with plain xr.open_zarr,
    which can't read an input given as a zipped Zarr.
    """
    import numpy as np
    import xarray as xr

    lat_dim, lon_dim = detect_spatial_dims(target)
    lat, lon = target[lat_dim].values, target[lon_dim].values
    grid = xr.Dataset(
        {"grid": (("latitude", "longitude"), np.zeros((lat.size, lon.size), dtype="float32"))},
        coords={"latitude": lat, "longitude": lon},
    )
    path = Path(workdir) / "grid.zarr"
    grid.to_zarr(path, mode="w", consolidated=True)
    return path


def _regrid(path, variable, reference, finer, workdir, label):
    """Put one input on the reference grid with coarsen (finer input) or downscale (coarser)."""
    import xarray as xr

    if finer:
        script = _SKILLS / "coarsen" / "scripts" / "coarsen.py"
        args = ["--method", "conservative"]
    else:
        script = _SKILLS / "downscale" / "scripts" / "downscale.py"
        args = ["--algorithm", "linear-interpolation"]
    if not script.is_file():
        # coarsen and downscale live in weather-skills/weather-skills; the weather-skills
        # plugin installs them beside this skill, a checkout of this repo alone does not.
        raise UsageError(
            f"{label} is on another grid, and regridding it needs the "
            f"{script.parent.parent.name} skill beside blend ({script}); install the "
            "weather-skills plugin, or copy skills/coarsen and skills/downscale from "
            "https://github.com/weather-skills/weather-skills next to skills/blend"
        )
    out = Path(workdir) / f"{label}.zarr"
    cmd = [
        "uv",
        "run",
        "--quiet",
        str(script),
        "-i",
        str(path),
        "-o",
        str(out),
        "--reference-grid",
        str(reference),
        "-v",
        variable,
        *args,
    ]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise UsageError(f"{label}: {script.parent.parent.name} failed:\n{run.stderr.strip()}")
    return xr.open_zarr(out)[variable].load()


def _parse_leads(values):
    """--lead values such as 2, 1-3 or 1,2,4 -> sorted 1-based leads."""
    leads = set()
    for value in values or ["1"]:
        for part in str(value).replace(" ", "").split(","):
            first, _, last = part.partition("-")
            try:
                lo, hi = int(first), int(last or first)
            except ValueError:
                raise UsageError(f"--lead {value}: give leads as 2, 1-3 or 1,2,4") from None
            if lo < 1 or hi < lo:
                raise UsageError(f"--lead {value}: leads start at 1 and ranges run upwards")
            leads.update(range(lo, hi + 1))
    return sorted(leads)


def _read_weights(text):
    """--weights as a dictionary: {label: weight}, or {lead: {label: weight}} for one set per lead.

    Takes blend-interface's downloaded file or weights line, JSON, or name=value pairs.
    """
    raw = str(text).strip()
    # Anything that is not JSON or name=value pairs is a file path.
    if not raw.startswith("{") and "=" not in raw and "\n" not in raw:
        if not Path(raw).is_file():
            raise UsageError(f"--weights {raw}: no such file, and not name=value pairs")
        raw = Path(raw).read_text().strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise UsageError(f"--weights: not valid JSON ({exc})") from None
        given = parsed.get("weights", parsed)
    else:
        given = dict(p.split("=", 1) for p in raw.replace("\n", ",").split(",") if p.strip())
    nested = bool(given) and all(isinstance(v, dict) for v in given.values())
    try:
        if nested:
            return {
                _lead_number(lead): {str(k).strip(): float(v) for k, v in ws.items()}
                for lead, ws in given.items()
            }
        return {str(k).strip(): float(v) for k, v in given.items()}
    except (TypeError, ValueError):
        raise UsageError(
            "--weights: every weight must be a number, and per-lead weights keyed by week1, "
            "week2, ... (or lead1, ...)"
        ) from None


def _lead_number(key):
    """A per-lead weights key, week2, lead2 or 2, as the lead number 2."""
    match = re.fullmatch(r"(?:week|lead)?\s*(\d+)", str(key).strip().lower())
    if not match:
        raise ValueError(key)
    return int(match.group(1))


def _lead_weights(given, lead, labels, absent, name):
    """One lead's weights for labels, from _read_weights' dictionary; sums to 1."""
    import numpy as np

    if given and all(isinstance(v, dict) for v in given.values()):
        if lead not in given:
            raise UsageError(f"--weights has no weights for {name}")
        given = given[lead]
    where = f" for {name}"
    unknown = sorted(set(given) - set(labels) - set(absent))
    if unknown:
        raise UsageError(
            f"--weights{where} names {unknown}, which are not inputs; the forecasts are {labels}"
        )
    dropped = [k for k in absent if given.get(k, 0) > 0]
    if dropped:
        raise UsageError(f"--weights gives weight to {dropped}, which lack {name}; set them to 0")
    missing = [k for k in labels if k not in given]
    if missing:
        raise UsageError(
            f"--weights{where} has no weight for {missing}; give every forecast one (0 leaves it out)"
        )
    w = np.array([given[k] for k in labels])
    if not np.isfinite(w).all() or (w < 0).any() or w.sum() <= 0:
        raise UsageError(f"--weights{where} must be 0 or more, with at least one above 0")
    if abs(w.sum() - 1) > 0.01:
        print(
            f"blend: {name} weights add up to {w.sum():.3f}; scaling them to 1",
            file=sys.stderr,
        )
    return w / w.sum()


@weather_skill(name="blend", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), action="append", required=True)
@weather_skill.argument("--label", action="append")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lead", action="append")
@weather_skill.argument("--weights")
@weather_skill.argument("--reference-grid", type=Dataset("spatial"))
@weather_skill.argument("--keep-members", action="store_true")
def blend(ds, label, variable, lead, weights, reference_grid, keep_members, **kwargs):
    """Blend several forecasts at one or more leads into a weighted mean on a common grid."""
    import numpy as np
    import xarray as xr

    if len(ds) < 2:
        raise UsageError(f"pass at least two --input forecasts, got {len(ds)}")
    if label and len(label) != len(ds):
        raise UsageError(
            f"got {len(label)} --label for {len(ds)} --input; pass one per input or none"
        )
    leads = _parse_leads(lead)
    paths = [Path(d.attrs[_INPUT_PATH_ATTR]) for d in ds]
    labels = list(label) if label else [_default_label(p) for p in paths]
    if len(set(labels)) != len(labels):
        raise UsageError(f"labels must differ, got {labels}; set them with --label")
    ds = [dequantify_dataset(ensure_normalized_longitude(d)) for d in ds]

    if variable is None:
        shared = set.intersection(*(set(d.data_vars) for d in ds))
        if len(shared) != 1:
            raise UsageError(
                f"inputs share {sorted(shared) or 'no'} data variables; pick one with --variable"
            )
        variable = shared.pop()
    for lab, d in zip(labels, ds, strict=True):
        if variable not in d.data_vars:
            raise UsageError(f"{lab}: no variable {variable!r} (has {list(d.data_vars)})")

    # Leads are named for what they are: week1, week2, ... for weekly inputs, else lead1, ...
    period = ds[0][variable].attrs.get("aggregation_period", "")
    word = "week" if period == "7 day" else "lead"

    def key(n):
        return f"{word}{n}"

    # Which inputs reach each lead; an input without a lead axis counts for lead 1 only.
    def reaches(d, dim, n):
        return n == 1 if dim is None else n <= d.sizes[dim]

    dims = [_lead_dim(d[variable]) for d in ds]
    absent = {
        n: [lab for lab, d, dim in zip(labels, ds, dims, strict=True) if not reaches(d, dim, n)]
        for n in leads
    }
    for n in leads:
        if absent[n]:
            print(
                f"blend: {word} {n} missing from {', '.join(absent[n])}; left out", file=sys.stderr
            )
        if len(absent[n]) == len(labels):
            raise UsageError(f"no input reaches {word} {n}")
    kept = [
        (lab, path, d[variable], dim)
        for lab, path, d, dim in zip(labels, paths, ds, dims, strict=True)
        if any(reaches(d, dim, n) for n in leads)
    ]

    # Target grid: --reference-grid, else the grid most inputs share (ties: the earliest).
    if reference_grid is not None:
        target = reference_grid
    else:
        keys = [_grid_key(da) for _, _, da, _ in kept]
        best = Counter(keys).most_common(1)[0][1]
        target = kept[next(k for k, key in enumerate(keys) if Counter(keys)[key] == best)][2]
    target_key, target_res = _grid_key(target), _spacing(target)
    lat_t, lon_t = (np.asarray(v) for v in target_key)

    # Each input on the target grid, one field per lead (NaN for a lead it lacks).
    names = [lab for lab, *_ in kept]
    fields = {}
    with tempfile.TemporaryDirectory(prefix="blend-") as work:
        ref_path = _write_grid(target, work)
        for lab, path, da, dim in kept:
            if _grid_key(da) != target_key:
                da = _regrid(path, variable, ref_path, _spacing(da) < target_res * 0.99, work, lab)
            lat_dim, lon_dim = detect_spatial_dims(da)
            da = da.rename({lat_dim: "latitude", lon_dim: "longitude"})
            # Regridding keeps only the input's extent; pad it back out to the full grid.
            da = da.reindex(latitude=lat_t, longitude=lon_t, method="nearest", tolerance=1e-3)
            for n in leads:
                if lab not in absent[n]:
                    fields[lab, n] = (da if dim is None else da.isel({dim: n - 1})).load()
    template = next(iter(fields.values()))
    attrs = {k: v for k, v in template.attrs.items() if not k.startswith("GRIB_")}

    def plain(f):
        return f.drop_vars([c for c in f.coords if c not in ("latitude", "longitude")])

    blends, stacks, weight_maps = [], [], {}
    given = _read_weights(weights) if weights else None
    for n in leads:
        here = [lab for lab in names if lab not in absent[n]]
        w = (
            _lead_weights(given, n, here, absent[n], f"{word} {n}")
            if given is not None
            else np.full(len(here), 1 / len(here))
        )
        stack = xr.concat(
            [plain(fields[lab, n]) for lab in here],
            dim=xr.DataArray(here, dims="model", name="model"),
        )
        weight = xr.DataArray(w, dims="model", coords={"model": here})
        # NaN wherever a forecast with weight has no value, so the blend never quietly
        # becomes a mean of fewer forecasts.
        covered = stack.sel(model=weight["model"][weight > 0]).notnull().all("model")
        blends.append((stack.fillna(0) * weight).sum("model").where(covered))
        stacks.append(stack.reindex(model=names))
        given_w = dict(zip(here, w, strict=True))
        weight_maps[n] = {k: round(float(given_w.get(k, 0.0)), 4) for k in names}

    # Keyed by lead, as blend-interface downloads them: {"week1": {label: weight}, ...}.
    weights_attr = json.dumps({key(n): weight_maps[n] for n in leads})
    blended = xr.concat(blends, dim="lead") if len(leads) > 1 else blends[0]
    blended.attrs = dict(
        attrs,
        long_name=f"{attrs.get('long_name') or variable} blend",
        blend_weights=weights_attr,
    )
    out = xr.Dataset({variable: blended})
    if keep_members:
        members = xr.concat(stacks, dim="lead") if len(leads) > 1 else stacks[0]
        out[f"{variable}_members"] = members.assign_attrs(attrs)
    # The lead axis holds, for each lead, the first input's value there (such as the
    # week's start date), from the first input that has a lead axis.
    lead_dim = next((dim for *_, dim in kept if dim is not None), None)
    if lead_dim is not None:
        values = [
            next(
                d[lead_dim].values[n - 1]
                for d, dim in zip(ds, dims, strict=True)
                if dim == lead_dim and n <= d.sizes[dim] and lead_dim in d.coords
            )
            for n in leads
        ]
        out = out.rename({"lead": lead_dim}) if len(leads) > 1 else out.expand_dims(lead_dim)
        out = out.assign_coords({lead_dim: values})
        out[lead_dim].attrs = ds[dims.index(lead_dim)][lead_dim].attrs
    span = f"{word} {leads[0]}" if len(leads) == 1 else f"{word}s {','.join(map(str, leads))}"
    # Stated in full: otherwise the first input's title, source and licence pass through.
    licences = dict.fromkeys(str(d.attrs["license"]) for d in ds if d.attrs.get("license"))
    out.attrs = {
        "title": f"Blend of {len(names)} forecasts, {span}",
        "source": ", ".join(names),
        "weather_skills_source": "blend:" + ",".join(names),
        "license": " | ".join(licences) or "see the inputs",
        "weather_skills_forecast_init": ds[0].attrs.get("weather_skills_forecast_init", ""),
        "blend_leads": json.dumps(leads),
        "blend_weights": weights_attr,
        "blend_left_out": json.dumps({key(n): absent[n] for n in leads}),
    }
    print(f"blend: {span}, weights {weights_attr}", file=sys.stderr)
    return out


if __name__ == "__main__":
    blend()

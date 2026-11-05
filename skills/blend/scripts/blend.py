# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
# ]
# ///
"""Blend several forecasts at one lead into one weighted mean on a common grid."""

import json
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
        raise UsageError(f"blend needs the {script.parent.parent.name} skill beside it ({script})")
    out = Path(workdir) / f"{label}.zarr"
    cmd = ["uv", "run", "--quiet", str(script), "-i", str(path), "-o", str(out),
           "--reference-grid", str(reference), "-v", variable, *args]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise UsageError(f"{label}: {script.parent.parent.name} failed:\n{run.stderr.strip()}")
    return xr.open_zarr(out)[variable].load()


def _parse_weights(text, labels, absent):
    """Weights from the interface's Finish text or file, JSON, or name=value pairs; sums to 1."""
    import numpy as np

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
    try:
        given = {str(k).strip(): float(v) for k, v in given.items()}
    except (TypeError, ValueError):
        raise UsageError("--weights: every weight must be a number") from None

    unknown = sorted(set(given) - set(labels) - set(absent))
    if unknown:
        raise UsageError(f"--weights names {unknown}, which are not inputs; the forecasts are {labels}")
    dropped = [k for k in absent if given.get(k, 0) > 0]
    if dropped:
        raise UsageError(f"--weights gives weight to {dropped}, which lack this lead; set them to 0")
    missing = [k for k in labels if k not in given]
    if missing:
        raise UsageError(f"--weights has no weight for {missing}; give every forecast one (0 leaves it out)")
    w = np.array([given[k] for k in labels])
    if not np.isfinite(w).all() or (w < 0).any() or w.sum() <= 0:
        raise UsageError("--weights must be 0 or more, with at least one above 0")
    if abs(w.sum() - 1) > 0.01:
        print(f"blend: weights add up to {w.sum():.3f}; scaling them to 1", file=sys.stderr)
    return w / w.sum()


@weather_skill(name="blend", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), action="append", required=True)
@weather_skill.argument("--label", action="append")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lead", type=int, default=1)
@weather_skill.argument("--weights")
@weather_skill.argument("--reference-grid", type=Dataset("spatial"))
@weather_skill.argument("--keep-members", action="store_true")
def blend(ds, label, variable, lead, weights, reference_grid, keep_members, **kwargs):
    """Blend several forecasts at one lead into one weighted mean on a common grid."""
    import numpy as np
    import xarray as xr

    if len(ds) < 2:
        raise UsageError(f"pass at least two --input forecasts, got {len(ds)}")
    if label and len(label) != len(ds):
        raise UsageError(f"got {len(label)} --label for {len(ds)} --input; pass one per input or none")
    paths = [Path(d.attrs[_INPUT_PATH_ATTR]) for d in ds]
    labels = list(label) if label else [_default_label(p) for p in paths]
    if len(set(labels)) != len(labels):
        raise UsageError(f"labels must differ, got {labels}; set them with --label")
    ds = [dequantify_dataset(ensure_normalized_longitude(d)) for d in ds]

    if variable is None:
        shared = set.intersection(*(set(d.data_vars) for d in ds))
        if len(shared) != 1:
            raise UsageError(f"inputs share {sorted(shared) or 'no'} data variables; pick one with --variable")
        variable = shared.pop()
    for lab, d in zip(labels, ds, strict=True):
        if variable not in d.data_vars:
            raise UsageError(f"{lab}: no variable {variable!r} (has {list(d.data_vars)})")

    # Keep the inputs that reach this lead.
    kept, absent = [], []
    for lab, path, d in zip(labels, paths, ds, strict=True):
        dim = _lead_dim(d[variable])
        if dim is not None and not 1 <= lead <= d.sizes[dim]:
            absent.append(lab)
            continue
        kept.append((lab, path, d[variable], dim))
    if absent:
        print(f"blend: lead {lead} missing from {', '.join(absent)}; left out", file=sys.stderr)
    if len(kept) < 2:
        raise UsageError(f"only {len(kept)} input(s) reach lead {lead}; need at least two")

    # Target grid: --reference-grid, else the grid most inputs share (ties: the earliest).
    if reference_grid is not None:
        target = reference_grid
    else:
        keys = [_grid_key(da) for _, _, da, _ in kept]
        best = Counter(keys).most_common(1)[0][1]
        target = kept[next(k for k, key in enumerate(keys) if Counter(keys)[key] == best)][2]
    target_key, target_res = _grid_key(target), _spacing(target)
    lat_t, lon_t = (np.asarray(v) for v in target_key)

    fields = []
    with tempfile.TemporaryDirectory(prefix="blend-") as work:
        ref_path = _write_grid(target, work)
        for lab, path, da, dim in kept:
            if _grid_key(da) != target_key:
                da = _regrid(path, variable, ref_path, _spacing(da) < target_res * 0.99, work, lab)
            if dim is not None:
                da = da.isel({dim: lead - 1})
            lat_dim, lon_dim = detect_spatial_dims(da)
            da = da.rename({lat_dim: "latitude", lon_dim: "longitude"})
            # Regridding keeps only the input's extent; pad it back out to the full grid.
            da = da.reindex(latitude=lat_t, longitude=lon_t, method="nearest", tolerance=1e-3)
            fields.append(da.load())

    names = [lab for lab, *_ in kept]
    lead_coord = kept[0][3]
    template = fields[0]
    stack = xr.concat(
        [f.drop_vars([c for c in f.coords if c not in ("latitude", "longitude")]) for f in fields],
        dim=xr.DataArray(names, dims="model", name="model"),
    )
    w = _parse_weights(weights, names, absent) if weights else np.full(len(names), 1 / len(names))
    weight = xr.DataArray(w, dims="model", coords={"model": names})
    # NaN wherever a forecast with weight has no value, so the blend never quietly
    # becomes a mean of fewer forecasts.
    covered = stack.sel(model=weight["model"][weight > 0]).notnull().all("model")
    blended = (stack.fillna(0) * weight).sum("model").where(covered)

    weight_map = {k: round(float(x), 4) for k, x in zip(names, w, strict=True)}
    attrs = {k: v for k, v in template.attrs.items() if not k.startswith("GRIB_")}
    blended.attrs = dict(attrs, long_name=f"{attrs.get('long_name') or variable} blend",
                         blend_weights=json.dumps(weight_map))
    out = xr.Dataset({variable: blended})
    if keep_members:
        out[f"{variable}_members"] = stack.assign_attrs(attrs)
    if lead_coord is not None and lead_coord in template.coords:
        out = out.expand_dims({lead_coord: [template[lead_coord].values]})
    # Stated in full: otherwise the first input's title, source and licence pass through.
    licences = dict.fromkeys(str(d.attrs["license"]) for d in ds if d.attrs.get("license"))
    out.attrs = {
        "title": f"Blend of {len(names)} forecasts, lead {lead}",
        "source": ", ".join(names),
        "weather_skills_source": "blend:" + ",".join(names),
        "license": " | ".join(licences) or "see the inputs",
        "weather_skills_forecast_init": ds[0].attrs.get("weather_skills_forecast_init", ""),
        "blend_lead": lead,
        "blend_weights": json.dumps(weight_map),
        "blend_left_out": json.dumps(absent),
    }
    print(f"blend: lead {lead}, weights {weight_map}", file=sys.stderr)
    return out


if __name__ == "__main__":
    blend()

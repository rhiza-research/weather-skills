# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core[dask] @ git+https://github.com/weather-skills/weather-skills-core@feat/dask-larger-than-memory",
#   "pandas",
# ]
# ///
"""Print a short summary of a Zarr: dimensions, coordinates, and each variable's units and value range."""

import json
import math

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.6"

# Data arrays can be huge. Stats scan the array but the printed sample never
# exceeds this many values, even when --max-values is 0 (which still dumps
# every *coordinate* value).
_DATA_SAMPLE_CAP = 256
# Variable attrs worth showing: what the values are and how they were made.
_KEY_ATTRS = (
    "long_name",
    "standard_name",
    "cell_methods",
    "aggregation_period",
    "data_interval",
)


def _plain(da):
    return da.pint.magnitude if getattr(getattr(da, "pint", None), "units", None) else da


def _numpy(da):
    data = _plain(da)
    data = data.values if hasattr(data, "values") else data
    return np.asarray(data.compute() if hasattr(data, "compute") else data)


def _units(da):
    u = getattr(getattr(da, "pint", None), "units", None)
    if u is not None:
        return f"{u:~cf}"
    return da.attrs.get("units") or None


def _fmt(v):
    if isinstance(v, np.datetime64):
        s = str(np.datetime_as_string(v, timezone="naive"))
        return s.split("T", 1)[0] if "T00:00:00" in s else s
    if isinstance(v, np.timedelta64):
        # Lead times as "7 days" / "0 days 03:00:00", not raw nanoseconds.
        import pandas as pd

        td = pd.Timedelta(v)
        if pd.isna(td):
            return "NaT"
        if td % pd.Timedelta(days=1) == pd.Timedelta(0):
            return f"{td.days} day" if td.days == 1 else f"{td.days} days"
        return str(td)
    v = v.item() if isinstance(v, np.generic) else v
    return v if type(v) in (int, float, str, bool) else str(v)


def _jsonable(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _show(v):
    if v is None:
        return "nan"
    if isinstance(v, float):
        return "nan" if not math.isfinite(v) else f"{v:g}"
    return str(v)


def _preview(vals, n):
    if n == 0 or len(vals) <= n:
        return list(vals), False
    h = max(1, n // 2)
    t = n - h
    if t <= 0:
        return [vals[0], "…"], True
    return [*vals[:h], "…", *vals[-t:]], True


def _finite(values):
    kind = values.dtype.kind
    if kind == "f":
        return np.isfinite(values)
    return ~np.isnat(values) if kind in "mM" else np.ones(values.shape, dtype=bool)


def _sample(da, want, skip_nan):
    """The first ``want`` values of ``da`` in C order (finite ones with ``skip_nan``).

    Reads leading slabs only: enough rows for ``want`` values, then (with ``skip_nan``)
    on to the next slab until ``want`` finite ones are found.
    """
    if not da.ndim:
        da = da.expand_dims("_")
    per_row = max(1, da.size // max(1, da.shape[0]))
    step = max(1, (1_000_000 if skip_nan else want + per_row - 1) // per_row)
    sample = []
    for start in range(0, da.shape[0], step):
        flat = da.isel({da.dims[0]: slice(start, start + step)}).values.reshape(-1)
        if skip_nan:
            flat = flat[_finite(flat)]
        sample.extend(_fmt(v) for v in flat[: want - len(sample)])
        if not skip_nan or len(sample) >= want:
            break
    return sample


def _data_var(name, da, max_values, skip_nan):
    plain = da.pint.dequantify()
    vals = plain
    want = _DATA_SAMPLE_CAP if max_values == 0 else min(max_values, _DATA_SAMPLE_CAP)
    n = int(vals.size)
    kind = vals.dtype.kind
    stats = {"n": n, "n_finite": None, "n_nan": None, "min": None, "max": None, "mean": None}
    if kind in "biufmM" and n:
        # Count, range and total in one pass over the data, however it is chunked.
        if kind == "f":
            vals = vals.where(np.isfinite(vals))
        lazy = {"n_finite": vals.count(), "lo": vals.min(skipna=True), "hi": vals.max(skipna=True)}
        if kind not in "mM":
            lazy["total"] = vals.sum(dtype="float64")
        found = xr.Dataset(lazy).compute()
        n_finite = int(found.n_finite)
        stats["n_finite"], stats["n_nan"] = n_finite, n - n_finite
        if n_finite:
            stats["min"] = _jsonable(_fmt(found.lo.values[()]))
            stats["max"] = _jsonable(_fmt(found.hi.values[()]))
            if kind not in "mM":
                stats["mean"] = _jsonable(float(found.total) / n_finite)
    if skip_nan and stats["n_finite"] is not None:
        sample = _sample(plain, want, skip_nan=True)
        truncated = stats["n_finite"] > len(sample)
    else:
        # The first values in order, NaN included, so they line up with the coordinates.
        sample = _sample(plain, want, skip_nan=False)
        truncated = n > len(sample)
    if truncated:
        sample.append("…")
    return {
        "name": name,
        "dims": list(da.dims),
        "shape": [int(s) for s in da.shape],
        "dtype": str(da.dtype),
        "units": _units(da),
        "attrs": {k: str(da.attrs[k]) for k in _KEY_ATTRS if da.attrs.get(k) is not None},
        **stats,
        "sample": [_jsonable(v) for v in sample],
        "sample_truncated": truncated,
    }


@weather_skill(
    name="inspect-zarr",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--format", choices=["human", "json"], default="human")
@weather_skill.argument(
    "--max-values",
    type=int,
    default=24,
    help="Max values printed per coordinate and per data-variable sample (default 24). "
    "0 prints every coordinate value; data samples still cap at 256.",
)
@weather_skill.argument(
    "--skip-nan",
    action="store_true",
    help="Sample only finite values (first ones found) instead of the first cells with NaN.",
)
def inspect_zarr(ds, format, max_values, skip_nan, **kwargs):
    """Print a short summary of a Zarr: dimensions, coordinates, and each variable's units and value range."""
    if max_values < 0:
        raise UsageError(
            "--max-values must be >= 0 (0 prints every coordinate value; data samples stay capped)."
        )

    dims = {str(k): int(v) for k, v in ds.sizes.items()}
    coords = []
    for name, da in ds.coords.items():
        arr = _numpy(da).reshape(-1)
        values, truncated = _preview([_fmt(v) for v in arr], max_values)
        coords.append(
            {
                "name": name,
                "dims": list(da.dims),
                "dtype": str(arr.dtype),
                "units": _units(da),
                "size": int(arr.size),
                "values": values,
                "truncated": truncated,
            }
        )
    data_vars = [_data_var(name, da, max_values, skip_nan) for name, da in ds.data_vars.items()]
    # Dataset-level attrs, minus the processing history (the provenance skill reads that).
    attrs = {str(k): str(v) for k, v in ds.attrs.items() if k != "weather_skills_history"}

    if format == "json":
        print(
            json.dumps(
                {"dims": dims, "coords": coords, "data_vars": data_vars, "attrs": attrs},
                indent=2,
            )
        )
        return

    print("Dimensions:")
    for name, size in dims.items():
        print(f"  {name}: {size}")
    print("\nCoordinates:")
    for c in coords:
        units = f" [{c['units']}]" if c["units"] else ""
        dims_s = ", ".join(c["dims"]) or "scalar"
        extra = f"  ({c['size']} values)" if c["truncated"] else ""
        shown = ", ".join(_show(v) for v in c["values"])
        print(f"  {c['name']} ({dims_s}) {c['dtype']}{units}")
        print(f"    {shown}{extra}")
    print("\nData variables:")
    for v in data_vars:
        units = f" [{v['units']}]" if v["units"] else ""
        shape = " × ".join(map(str, v["shape"])) or "scalar"
        print(f"  {v['name']} ({', '.join(v['dims'])}) {v['dtype']} {shape}{units}")
        for key, val in v["attrs"].items():
            print(f"    {key}: {val}")
        if v["n_finite"] is not None:
            mean = f"  mean {_show(v['mean'])}" if v["mean"] is not None else ""
            print(
                f"    finite {v['n_finite']}/{v['n']}  "
                f"min {_show(v['min'])}  max {_show(v['max'])}{mean}"
            )
        shown = ", ".join(_show(x) for x in v["sample"]) or "(no finite values)"
        of = (
            f"{v['n_finite']} finite cells"
            if skip_nan and v["n_finite"] is not None
            else f"{v['n']} cells"
        )
        extra = f"  ({len(v['sample']) - 1} of {of})" if v["sample_truncated"] else ""
        print(f"    sample: {shown}{extra}")
    if attrs:
        print("\nAttributes:")
        for key, val in attrs.items():
            print(f"  {key}: {val}")


if __name__ == "__main__":
    inspect_zarr()

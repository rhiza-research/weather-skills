# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
# ]
# ///
"""Print a short summary of a Zarr: dimensions, coordinates, and each variable's units and value range."""

import json
import math

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

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


def _data_var(name, da, max_values, skip_nan):
    arr = _numpy(da)
    want = _DATA_SAMPLE_CAP if max_values == 0 else min(max_values, _DATA_SAMPLE_CAP)
    n = int(arr.size)
    kind = arr.dtype.kind
    stats = {"n": n, "n_finite": None, "n_nan": None, "min": None, "max": None, "mean": None}
    sample = []
    if kind in "biufmM" and n:
        # One pass over blocks along the first axis: no full-size copies of a
        # large array.
        n_finite, total, lo, hi = 0, 0.0, None, None
        blocks = arr.reshape(arr.shape[0], -1) if arr.ndim else arr.reshape(1, 1)
        step = max(1, 10_000_000 // max(1, blocks.shape[1]))
        for start in range(0, blocks.shape[0], step):
            block = blocks[start : start + step].reshape(-1)
            if kind == "f":
                block = block[np.isfinite(block)]
            elif kind in "mM":
                block = block[~np.isnat(block)]
            if not block.size:
                continue
            n_finite += int(block.size)
            lo = block.min() if lo is None else min(lo, block.min())
            hi = block.max() if hi is None else max(hi, block.max())
            if kind not in "mM":
                total += float(block.sum(dtype="float64"))
            if skip_nan and len(sample) < want:
                sample.extend(_fmt(v) for v in block[: want - len(sample)])
        stats["n_finite"], stats["n_nan"] = n_finite, n - n_finite
        if n_finite:
            stats["min"], stats["max"] = _jsonable(_fmt(lo)), _jsonable(_fmt(hi))
            if kind not in "mM":
                stats["mean"] = _jsonable(total / n_finite)
    if skip_nan and stats["n_finite"] is not None:
        truncated = stats["n_finite"] > len(sample)
    else:
        # The first values in order, NaN included, so they line up with the coordinates.
        sample = [_fmt(v) for v in arr.reshape(-1)[:want]]
        truncated = n > len(sample)
    if truncated:
        sample.append("…")
    return {
        "name": name,
        "dims": list(da.dims),
        "shape": [int(s) for s in da.shape],
        "dtype": str(arr.dtype),
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

    if format == "json":
        print(json.dumps({"dims": dims, "coords": coords, "data_vars": data_vars}, indent=2))
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


if __name__ == "__main__":
    inspect_zarr()

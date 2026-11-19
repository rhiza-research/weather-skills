# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "pandas",
# ]
# ///
"""Put overlapping or irregular time samples onto a regular grid, averaging where they overlap."""

import math
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
    data_interval_of,
    format_cell_methods,
    format_duration,
    parse_aggregation_period,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# cell_methods comment on values built from overlapping samples; later skills keep it.
OVERLAPPING_SAMPLES = "overlapping samples"


def _ns(text, what):
    """A fixed duration in integer nanoseconds; calendar months and years have no fixed length."""
    if any(word in text for word in ("month", "year")):
        raise UsageError(f"{what} {text!r} is a calendar length; resample-time needs a fixed one")
    try:
        ns = round(float(parse_aggregation_period(text).to("ns").magnitude))
    except UsageError as exc:
        raise UsageError(
            f"{what} {text!r} is not a duration (e.g. '1 day', '15 minute'): {exc}"
        ) from None
    if ns <= 0:
        raise UsageError(f"{what} {text!r} must be positive")
    return ns


@weather_skill(
    name="resample-time",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--period",
    required=True,
    help="Output grid spacing: a fixed duration such as '1 day', '6 hour' or '15 minute'.",
)
@weather_skill.argument("--time-dim")
def resample_time(ds, period, time_dim, **kwargs):
    """Put overlapping or irregular time samples onto a regular grid, averaging where they overlap."""
    import numpy as np
    import xarray as xr

    dim = time_dim or ("time" if "time" in ds.dims and ds.sizes["time"] > 1 else "step")
    if dim not in ds.dims:
        raise UsageError(f"no {dim!r} axis to resample (dims: {list(ds.dims)})")
    raw = np.asarray(ds[dim].values)
    # shortcut: cftime model calendars are refused; add them if a task needs one.
    if raw.dtype.kind not in "Mm":
        raise UsageError(f"{dim} must be datetime64 or timedelta64 (got {raw.dtype})")
    period_ns = _ns(period, "--period")
    period_label = format_duration(parse_aggregation_period(period))

    # Each sample covers [start, end): its CF bounds, or [t, t + its own interval).
    bounds_name = ds[dim].attrs.get("bounds")
    if not (isinstance(bounds_name, str) and bounds_name in ds):
        bounds_name = f"{dim}_bounds" if f"{dim}_bounds" in ds else None
    if bounds_name is not None:
        bounds = np.asarray(ds[bounds_name].values)
        starts = bounds[:, 0].astype(f"{raw.dtype.kind}8[ns]").astype("int64")
        ends = bounds[:, 1].astype(f"{raw.dtype.kind}8[ns]").astype("int64")
    else:
        cell = next(
            (
                ds[n].attrs[AGGREGATION_PERIOD_ATTR]
                for n in ds.data_vars
                if isinstance(ds[n].attrs.get(AGGREGATION_PERIOD_ATTR), str)
            ),
            data_interval_of(ds),
        )
        if not cell:
            raise UsageError(
                f"samples on {dim} carry no interval: no {dim}_bounds, aggregation_period "
                "or data_interval, so their overlap is unknown"
            )
        starts = raw.astype(f"{raw.dtype.kind}8[ns]").astype("int64")
        ends = starts + _ns(cell, "sample interval")
    if (ends <= starts).any():
        raise UsageError(f"every sample on {dim} must end after it starts")

    # Grid edges sit on whole periods from 00 UTC (time) or lead 0 (step), and every
    # sample edge must fall on one, so each grid cell is wholly inside or outside it.
    origin = 0 if raw.dtype.kind == "m" else starts.min() - starts.min() % 86_400_000_000_000
    offsets = np.concatenate([starts, ends]) - origin
    if (offsets % period_ns).any():
        fits = math.gcd(*(int(v) for v in offsets))
        raise UsageError(
            f"--period {period} does not divide every sample edge on {dim}; the longest "
            f"period that does is {format_duration(parse_aggregation_period(f'{fits // 10**9} second'))}"
        )
    left = np.arange(starts.min(), ends.max(), period_ns, dtype="int64")
    inside = (starts[None, :] <= left[:, None]) & (left[:, None] + period_ns <= ends[None, :])

    # A sample counts toward coverage at its own coverage, and only if some cell has data.
    cov_in = np.ones(raw.size)
    if AGGREGATION_COVERAGE_COORD in ds.coords and ds[AGGREGATION_COVERAGE_COORD].dims == (dim,):
        cov_in = np.nan_to_num(ds[AGGREGATION_COVERAGE_COORD].values.astype(float))
    for name in ds.data_vars:
        da = ds[name]
        if dim in da.dims:
            cov_in = cov_in * np.isfinite(da).any([d for d in da.dims if d != dim]).values
    n_cover = inside.sum(axis=1)
    coverage = np.where(n_cover > 0, (inside * cov_in).sum(axis=1) / np.maximum(n_cover, 1), 0.0)

    out_dim = "_grid"
    weights = xr.DataArray(inside.astype(float), dims=(out_dim, dim))
    drop = [v for v in ds.variables if dim in ds[v].dims and v != dim and v not in ds.data_vars]
    ds = ds.drop_vars(drop + ([bounds_name] if bounds_name else []))
    overlapping = bool((n_cover > 1).any()) or any(
        OVERLAPPING_SAMPLES in str(ds[n].attrs.get("cell_methods", "")) for n in ds.data_vars
    )
    cell_methods = format_cell_methods(dim, "mean")
    if overlapping:
        cell_methods = f"{cell_methods} (comment: {OVERLAPPING_SAMPLES})"

    out = ds.drop_dims(dim)
    for name in ds.data_vars:
        da = ds[name]
        if dim not in da.dims:
            continue
        # Plain mean of every sample covering the grid cell.
        count = xr.dot(weights, da.notnull().astype(float), dim=dim)
        total = xr.dot(weights, da.fillna(0.0), dim=dim)
        mean = (total / count).where(count > 0)
        # A cell missing samples that other cells have is NaN, not a mean of what it has.
        others = [d for d in count.dims if d != out_dim]
        mean = mean.where(count >= count.max(others)) if others else mean
        mean = mean.rename({out_dim: dim}).transpose(*da.dims).astype(da.dtype, copy=False)
        attrs = {
            **da.attrs,
            AGGREGATION_PERIOD_ATTR: period_label,
            DATA_INTERVAL_ATTR: period_label,
            "cell_methods": cell_methods,
        }
        out[name] = mean.assign_attrs(attrs)

    labels = left.astype(f"{raw.dtype.kind}8[ns]")
    out = out.assign_coords({dim: (dim, labels, ds[dim].attrs)})
    out[dim].attrs.pop("bounds", None)
    out = out.assign_coords({AGGREGATION_COVERAGE_COORD: (dim, coverage)})
    out[AGGREGATION_COVERAGE_COORD].attrs.update(
        long_name="fraction of native samples with finite data in the interval",
        units="1",
        valid_min=0.0,
        valid_max=1.0,
    )
    empty = int((n_cover == 0).sum())
    print(
        f"{raw.size} samples -> {left.size} {period_label} "
        f"cells on {dim}; {int((n_cover > 1).sum())} averaged from overlapping samples, "
        f"{empty} with no sample (coverage 0)",
        file=sys.stderr,
    )
    return out


if __name__ == "__main__":
    resample_time()

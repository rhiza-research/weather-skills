# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.4.0",
#   "pandas",
# ]
# ///
"""Average overlapping or irregular time samples onto a regular grid."""

import math
import re
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
    cell_methods_has_sum,
    classify_variable,
    format_cell_methods,
    format_duration,
    parse_aggregation_period,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

# Added to cell_methods when a cell averages more than one sample; later skills keep it.
OVERLAPPING_SAMPLES = "overlapping samples"


@weather_skill(
    name="regularize-time",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--period",
    required=True,
    help="Output grid spacing: a fixed duration such as '1 day', '6 hour' or '15 minute'.",
)
@weather_skill.argument("--time-dim")
def regularize_time(ds, period, time_dim, **kwargs):
    """Average overlapping or irregular time samples onto a regular grid.

    Each --period cell is the mean of the samples that cover it, and
    aggregation_coverage says how complete those samples were.
    """
    import numpy as np
    import xarray as xr

    dim = time_dim or ("step" if "step" in ds.dims and ds.sizes.get("time", 1) == 1 else "time")
    if dim not in ds.dims:
        raise UsageError(f"no {dim!r} axis to regularize (dims: {list(ds.dims)})")
    if ds[dim].attrs.get("standard_name") == "forecast_reference_time":
        raise UsageError(
            f"{dim} holds forecast inits, not valid times; run step-to-time first so each "
            "sample sits on the dates it covers, or pass --time-dim step to regularize leads"
        )
    raw = np.asarray(ds[dim].values)
    # shortcut: cftime model calendars are refused; add them if a task needs one.
    if raw.dtype.kind not in "Mm":
        raise UsageError(f"{dim} must be datetime64 or timedelta64 (got {raw.dtype})")
    if np.isnat(raw).any():
        raise UsageError(f"{dim} has missing (NaT) labels")
    ns_type = f"{raw.dtype.kind}8[ns]"
    period_q = parse_aggregation_period(period)
    if str(period_q.units) in ("month", "year"):
        raise UsageError(f"--period {period!r} is a calendar length; use a fixed one")
    period_ns = round(float(period_q.to("ns").magnitude))
    if period_ns <= 0:
        raise UsageError(f"--period {period!r} must be positive")
    period_label = format_duration(period_q)

    on_axis = [name for name in ds.data_vars if dim in ds[name].dims]
    for name in on_axis:
        methods = str(ds[name].attrs.get("cell_methods", ""))
        if re.search(r":\s*(maximum|minimum)\b", methods) or cell_methods_has_sum(methods):
            raise UsageError(
                f"{name!r} has cell_methods {methods!r}; regularize-time averages, so it "
                "takes means or rates, not extremes or sums"
            )
        kind = classify_variable(
            name, units=variable_units(ds[name]), standard_name=ds[name].attrs.get("standard_name")
        )
        if kind == "precip_amount":
            raise UsageError(f"{name!r} is a precipitation amount; pass rates (mm day-1)")

    # Where each sample starts and ends: its CF bounds, or its label plus its interval.
    bounds_name = ds[dim].attrs.get("bounds")
    if not (isinstance(bounds_name, str) and bounds_name in ds):
        bounds_name = f"{dim}_bounds" if f"{dim}_bounds" in ds else None
    if bounds_name is not None:
        edges = ds[bounds_name].transpose(dim, ...).values
        if edges.dtype.kind != raw.dtype.kind or edges.shape != (raw.size, 2):
            raise UsageError(
                f"{bounds_name} must hold a start and end per {dim} label, as {dim} does"
            )
        if np.isnat(edges).any():
            raise UsageError(f"{bounds_name} has missing (NaT) edges")
        starts, ends = edges.astype(ns_type).astype("int64").T
        ds = ds.drop_vars(bounds_name)
    else:
        # Each variable's aggregation_period, else its data_interval.
        cells = {
            ds[name].attrs.get(AGGREGATION_PERIOD_ATTR) or ds[name].attrs.get(DATA_INTERVAL_ATTR)
            for name in on_axis
        } - {None}
        if len(cells) > 1:
            raise UsageError(
                f"variables on {dim} cover different intervals ({sorted(cells)}); "
                "regularize them one at a time"
            )
        cell = cells.pop() if cells else None
        if not cell:
            raise UsageError(
                f"samples on {dim} carry no interval: no {dim}_bounds, aggregation_period "
                "or data_interval, so their overlap is unknown"
            )
        if "month" in cell or "year" in cell:
            raise UsageError(f"samples on {dim} are {cell} long, a calendar length")
        starts = raw.astype(ns_type).astype("int64")
        ends = starts + round(float(parse_aggregation_period(cell).to("ns").magnitude))
    if (ends <= starts).any():
        raise UsageError(f"every sample on {dim} must end after it starts")

    # The grid starts at the earliest sample. Every sample edge must land on a cell edge,
    # so each cell lies wholly inside or outside each sample.
    offsets = np.concatenate([starts, ends]) - starts.min()
    if (offsets % period_ns).any():
        step_ns = math.gcd(*(int(v) for v in offsets))
        if step_ns % 10**9:
            raise UsageError(f"sample edges on {dim} are not whole seconds apart")
        fits = format_duration(parse_aggregation_period(f"{step_ns // 10**9} second"))
        raise UsageError(
            f"--period {period} does not divide every sample edge on {dim}; the longest "
            f"period that does is {fits}. regularize-time only splits samples: to average "
            f"up to a longer period, run it with --period '{fits}', then aggregate-temporal"
        )
    first = (starts - starts.min()) // period_ns
    stop = (ends - starts.min()) // period_ns
    left = starts.min() + np.arange(stop.max(), dtype="int64") * period_ns

    # Each sample's coverage: its own aggregation_coverage (or 1), or 0 if it holds no data.
    cov_in = np.ones(raw.size)
    if AGGREGATION_COVERAGE_COORD in ds.coords:
        # shortcut: one coverage per sample; per-point coverage needs per-point output too.
        if set(ds[AGGREGATION_COVERAGE_COORD].dims) - {dim}:
            raise UsageError(f"{AGGREGATION_COVERAGE_COORD} must vary along {dim} only")
        cov = ds[AGGREGATION_COVERAGE_COORD].values.astype(float)
        cov_in = np.nan_to_num(np.broadcast_to(cov, raw.shape))
    for name in ds.data_vars:
        da = ds[name]
        if dim in da.dims:
            cov_in = cov_in * np.isfinite(da).any([d for d in da.dims if d != dim]).values
    # Add each sample into the cells it covers; a cell's coverage is the mean of those.
    n_cover = np.zeros(left.size)
    cov_sum = np.zeros(left.size)
    for i in range(raw.size):
        n_cover[first[i] : stop[i]] += 1
        cov_sum[first[i] : stop[i]] += cov_in[i]
    coverage = np.where(n_cover > 0, cov_sum / np.maximum(n_cover, 1), 0.0)

    overlapping = bool((n_cover > 1).any()) or any(
        OVERLAPPING_SAMPLES in str(ds[n].attrs.get("cell_methods", "")) for n in ds.data_vars
    )
    cell_methods = format_cell_methods(dim, "mean")
    if overlapping:
        cell_methods = f"{cell_methods} (comment: {OVERLAPPING_SAMPLES})"

    # Drop everything on the old axis; each variable is rebuilt on the grid below.
    out = ds.drop_dims(dim)
    for name in ds.data_vars:
        da = ds[name]
        if dim not in da.dims:
            continue
        rest = [d for d in da.dims if d != dim]
        values = da.transpose(dim, *rest).values.astype(float)
        present = np.isfinite(values)
        values[~present] = 0.0
        # Mean of the samples covering each cell, skipping missing values.
        count = np.zeros((left.size, *values.shape[1:]))
        total = np.zeros((left.size, *values.shape[1:]))
        for i in range(raw.size):
            count[first[i] : stop[i]] += present[i]
            total[first[i] : stop[i]] += values[i]
        # A point missing a sample that other points in the cell have is NaN, not a
        # mean of fewer samples.
        full = count.reshape(left.size, -1).max(axis=1).reshape(-1, *[1] * len(rest))
        mean = np.where((count > 0) & (count >= full), total / np.maximum(count, 1), np.nan)
        mean = xr.DataArray(
            mean, dims=(dim, *rest), coords={d: da[d] for d in rest if d in da.coords}
        )
        mean = mean.transpose(*da.dims)
        if da.dtype.kind == "f":
            mean = mean.astype(da.dtype, copy=False)
        # Methods on other dims stay; this axis gets one mean.
        old = str(da.attrs.get("cell_methods", ""))
        others = re.sub(rf"\b{dim}:\s*\w+(\s*\([^)]*\))?", "", old).split()
        attrs = {
            **da.attrs,
            AGGREGATION_PERIOD_ATTR: period_label,
            DATA_INTERVAL_ATTR: period_label,
            "cell_methods": " ".join([*others, cell_methods]),
        }
        out[name] = mean.assign_attrs(attrs)

    dim_attrs = {k: v for k, v in ds[dim].attrs.items() if k != "bounds"}
    out = out.assign_coords({dim: (dim, left.astype(ns_type), dim_attrs)})
    out = out.assign_coords({AGGREGATION_COVERAGE_COORD: (dim, coverage)})
    out[AGGREGATION_COVERAGE_COORD].attrs.update(
        long_name="fraction of native samples with finite data in the interval",
        units="1",
        valid_min=0.0,
        valid_max=1.0,
    )
    print(
        f"{raw.size} samples -> {left.size} {period_label} cells on {dim}; "
        f"{int((n_cover > 1).sum())} averaged from overlapping samples, "
        f"{int((n_cover == 0).sum())} with no sample (coverage 0)",
        file=sys.stderr,
    )
    return out


if __name__ == "__main__":
    regularize_time()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
# ]
# ///
"""Roll up a Zarr along its time or forecast-step axis into fixed or rolling windows (mean, min, or max)."""

import datetime as _dt
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import roll_and_agg
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
    data_interval_of,
    dequantify_dataset,
    expected_samples_in_period,
    format_cell_methods,
    format_duration,
    infer_timestep,
    parse_aggregation_period,
    stamp_data_interval,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.5"

# Named --period → the aggregation_period string stamped for that window.
PERIOD_TO_AGGREGATION = {
    "daily": "1 day",
    "weekly": "7 day",
    "dekadal": "1 dekad",
    "monthly": "1 month",
}
PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10}
RESAMPLE_FREQ = {"daily": "1D", "weekly": "7D", "dekadal": "10D", "monthly": "MS"}
ANCHOR_PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10, "monthly": 30}
_METHOD_TO_CF = {"mean": "mean", "max": "maximum", "min": "minimum"}
_NAMED_PERIODS = tuple(PERIOD_TO_AGGREGATION)


def _resolve_period(period: str) -> dict:
    """Named period or a pint duration (``21 day``, ``6 hour``); ``anchor_days`` may be fractional."""
    if period in PERIOD_TO_AGGREGATION:
        return {
            "key": period,
            "agg": PERIOD_TO_AGGREGATION[period],
            "freq": RESAMPLE_FREQ[period],
            "anchor_days": ANCHOR_PERIOD_DAYS[period],
        }
    try:
        quantity = parse_aggregation_period(period)
    except UsageError as exc:
        raise UsageError(
            f"--period {period!r} must be one of {', '.join(_NAMED_PERIODS)} "
            f"or a pint duration (e.g. '21 day', '6 hour'): {exc}"
        ) from None
    days = float(quantity.to("day").magnitude)
    seconds = days * 86400
    if days <= 0 or abs(seconds - round(seconds)) > 1e-6:
        raise UsageError(
            f"--period {period!r} must be a named period "
            f"({', '.join(_NAMED_PERIODS)}) or a positive whole number of seconds "
            "(e.g. '21 day', '6 hour')"
        )
    if abs(days - round(days)) < 1e-6:
        return {
            "key": period,
            "agg": format_duration(quantity),
            "freq": f"{int(round(days))}D",
            "anchor_days": int(round(days)),
        }
    # Sub-daily: bins of this many seconds, aligned to 00 UTC of the first day.
    return {
        "key": period,
        "agg": format_duration(quantity),
        "freq": f"{int(round(seconds))}s",
        "anchor_days": days,
    }


def _durations_equal(left: str | None, right: str | None) -> bool:
    """True when two pint duration strings are the same length."""
    if not left or not right:
        return False
    try:
        return parse_aggregation_period(left) == parse_aggregation_period(right)
    except UsageError:
        return False


def _native_interval(ds, dim) -> str | None:
    stamped = data_interval_of(ds)
    if stamped:
        return stamped
    try:
        return format_duration(infer_timestep(ds, dim))
    except UsageError:
        return None


def _coverage_cell(ds) -> str | None:
    """Current cell width for coverage: re-agg uses aggregation_period, else native."""
    for name in ds.data_vars:
        val = ds[name].attrs.get(AGGREGATION_PERIOD_ATTR)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return data_interval_of(ds)


def _cf_bounds(ds, dim):
    """``(N, 2)`` start/end array if CF bounds exist, else None."""
    import numpy as np

    name = ds[dim].attrs.get("bounds") if dim in ds.coords or dim in ds.dims else None
    if not (isinstance(name, str) and name in ds):
        name = f"{dim}_bounds" if f"{dim}_bounds" in ds else None
    if name is None:
        return None
    arr = np.asarray(ds[name].values)
    if arr.ndim != 2 or arr.shape[-1] != 2:
        raise UsageError(f"{name!r} must have shape ({dim}, 2) start/end")
    return arr.reshape(arr.shape[0], 2)


def _as_ns(val):
    import numpy as np
    import pandas as pd

    # Anchored and monthly bin edges are pd.Timestamp; np.asarray makes those object dtype.
    if isinstance(val, (pd.Timestamp, pd.Timedelta)):
        val = val.to_numpy()
    arr = np.asarray(val)
    if arr.dtype.kind == "O" and hasattr(arr.flat[0], "calendar"):
        import cftime

        # Model calendars (noleap, 360_day): ns since a fixed epoch in that calendar.
        seconds = cftime.date2num(arr, "seconds since 1970-01-01", calendar=arr.flat[0].calendar)
        return np.round(np.asarray(seconds, dtype=float) * 1e9).astype(np.int64)
    if arr.dtype.kind == "m":
        return arr.astype("timedelta64[ns]").astype(np.int64)
    if arr.dtype.kind == "M":
        return arr.astype("datetime64[ns]").astype(np.int64)
    raise UsageError(f"cannot convert {arr.dtype} to a duration for CF bounds")


def _finite_along(ds, dim):
    """True at each ``dim`` index that is not all-NaN across other dims.

    A persistent spatial hole (land mask) does not mark the time as missing.
    An unpublished forecast lead that is NaN everywhere does. Every data
    variable must have some finite value at that index.
    """
    import numpy as np

    n = ds.sizes.get(dim, 0)
    if n == 0 or not list(ds.data_vars):
        return np.ones(n, dtype=bool)
    flags = []
    for name in ds.data_vars:
        da = ds[name]
        if getattr(da, "pint", None) is not None and da.pint.units is not None:
            da = da.pint.dequantify()
        if dim not in da.dims:
            continue
        values = np.asarray(da.values)
        axis = da.dims.index(dim)
        other = tuple(i for i in range(values.ndim) if i != axis)
        finite = np.isfinite(values)
        present = np.any(finite, axis=other) if other else finite
        flags.append(np.asarray(present, dtype=bool).reshape(n))
    if not flags:
        return np.ones(n, dtype=bool)
    return np.all(np.stack(flags, axis=0), axis=0)


def _present(ds, dim):
    """Per-sample presence: finite, times the input's own coverage when re-aggregating.

    A weekly bin that was 3/7 complete counts as 3/7 of a week, not a whole one.
    """
    import numpy as np

    present = _finite_along(ds, dim).astype(float)
    if AGGREGATION_COVERAGE_COORD in ds.coords and ds[AGGREGATION_COVERAGE_COORD].dims == (dim,):
        present *= np.nan_to_num(np.asarray(ds[AGGREGATION_COVERAGE_COORD].values, dtype=float))
    return present


def _drop_bounds(ds, dim):
    name = ds[dim].attrs.get("bounds") if dim in ds.coords or dim in ds.dims else None
    names = []
    if isinstance(name, str) and name in ds:
        names.append(name)
    extra = f"{dim}_bounds"
    if extra in ds and extra not in names:
        names.append(extra)
    if names:
        ds = ds.drop_vars(names)
    if dim in ds.coords:
        attrs = dict(ds[dim].attrs)
        attrs.pop("bounds", None)
        ds[dim].attrs = attrs
    return ds


def _weighted_mean(ds, dim, indices, weights):
    import numpy as np
    import xarray as xr

    w = np.asarray(weights, dtype=float)
    # dequantify_dataset keeps the standard spelling (mm day-1, not millimeter / day).
    sub = dequantify_dataset(_drop_bounds(ds.isel({dim: indices}), dim))
    wda = xr.DataArray(w, dims=[dim], coords={dim: sub[dim].values})
    # Weight only finite samples. Dividing by sum(weights) treats NaN as 0, which
    # turns unpublished GEFS long leads (all-NaN weeks 4–5) into fake dry zeros.
    numer = (sub * wda).sum(dim=dim, skipna=True, keep_attrs=True)
    denom = (sub.notnull() * wda).sum(dim=dim)
    # A cell missing samples other cells have is NaN, not a mean of what it has.
    return (numer / denom).where(denom >= denom.max())


def _empty_bins_message(spec, dim):
    return f"no {spec['key']} bins contained samples on {dim}"


def _aggregate_from_bounds(ds, dim, spec, method, bins):
    """Duration-weight samples into ``bins`` of ``(left, right, label)``."""
    import numpy as np
    import xarray as xr

    bounds = _cf_bounds(ds, dim)
    starts = _as_ns(bounds[:, 0])
    ends = _as_ns(bounds[:, 1])
    work = _drop_bounds(ds, dim)
    present = _finite_along(work, dim)
    chunks, labels, coverages = [], [], []
    for left, right, label in bins:
        left_ns = int(np.asarray(_as_ns(left)).reshape(-1)[0])
        right_ns = int(np.asarray(_as_ns(right)).reshape(-1)[0])
        period_ns = right_ns - left_ns
        if period_ns <= 0:
            continue
        overlap = np.minimum(ends, right_ns) - np.maximum(starts, left_ns)
        overlap = np.maximum(overlap, 0)
        idx = np.flatnonzero(overlap > 0)
        if idx.size == 0:
            continue
        covered = float((overlap * present)[idx].sum())
        coverage = min(1.0, covered / period_ns)
        if method == "mean":
            chunks.append(_weighted_mean(work, dim, idx, overlap[idx]))
        else:
            chunks.append(_reduce(work.isel({dim: idx}), method=method, dim=dim))
        labels.append(label)
        coverages.append(coverage)
    if not chunks:
        raise UsageError(_empty_bins_message(spec, dim))
    out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})
    return _assign_coverage(out, dim, coverages)


def _coverage_fraction(n_present: float, expected: int) -> float:
    if expected <= 0:
        return 1.0
    return min(1.0, n_present / expected)


def _assign_coverage(out, dim, coverages):
    import numpy as np

    if out.sizes.get(dim, 0) == 0:
        return out
    values = np.asarray(coverages, dtype=float)
    out = out.assign_coords({AGGREGATION_COVERAGE_COORD: (dim, values)})
    out[AGGREGATION_COVERAGE_COORD].attrs.update(
        long_name="fraction of native samples with finite data in the interval",
        units="1",
        valid_min=0.0,
        valid_max=1.0,
    )
    return out


def _reduce(grouped, method, dim=None):
    fn = {"mean": grouped.mean, "max": grouped.max, "min": grouped.min}[method]
    if dim is None:
        return fn(keep_attrs=True)
    # A cell missing samples other cells have is NaN, not a mean of what it has:
    # convert-to-totals would scale that mean up to the full period.
    count = grouped.notnull().sum(dim=dim)
    return fn(dim=dim, keep_attrs=True).where(count >= count.max())


def _group_indices(group, size: int):
    import numpy as np

    if isinstance(group, slice):
        return list(range(*group.indices(size)))
    return list(np.asarray(group).ravel())


def _timestep_days(ds, dim) -> float:
    try:
        return float(infer_timestep(ds, dim).to("day").magnitude)
    except UsageError:
        return 1.0


def _days_in_month(label) -> int:
    """Month length in the label's own calendar (cftime: noleap Feb is 28, 360_day is 30)."""
    if hasattr(label, "daysinmonth"):
        return int(label.daysinmonth)
    import pandas as pd

    return int(pd.Timestamp(label).days_in_month)


def _expected_samples(
    period: str, label, timestep_days: float, *, period_days: int | None = None
) -> int:
    """How many input samples a complete bin of ``period`` should hold."""
    if period_days is not None:
        days = period_days
    elif period == "monthly":
        days = _days_in_month(label)
    else:
        days = PERIOD_DAYS[period]
    return max(1, int(round(days / timestep_days)))


def _expected_from_interval(spec, label, native_interval, timestep_days: float) -> int:
    if native_interval:
        try:
            if spec["key"] == "monthly":
                period = f"{_days_in_month(label)} day"
            else:
                period = spec["agg"]
            return expected_samples_in_period(period, native_interval)
        except UsageError:
            pass
    period_days = None if spec["key"] == "monthly" else spec["anchor_days"]
    return _expected_samples(spec["key"], label, timestep_days, period_days=period_days)


def _aggregate_time_resample(ds, dim, spec, method):
    """Forward calendar resample; stamp ``aggregation_coverage`` on every bin with samples."""
    import numpy as np
    import xarray as xr

    freq = spec["freq"]
    if _cf_bounds(ds, dim) is not None:
        resampler = ds.resample({dim: freq})
        bins = []
        for label in resampler.groups:
            if spec["key"] == "monthly":
                # replace() works for pandas and cftime labels alike.
                year, month = (
                    (label.year + 1, 1) if label.month == 12 else (label.year, label.month + 1)
                )
                left, right = label, label.replace(year=year, month=month)
            elif hasattr(label, "calendar"):
                left = label
                right = label + _dt.timedelta(days=spec["anchor_days"])
            else:
                left = np.datetime64(label)
                right = left + np.timedelta64(int(round(spec["anchor_days"] * 86400)), "s")
            bins.append((left, right, left if hasattr(label, "calendar") else np.datetime64(label)))
        return _aggregate_from_bounds(ds, dim, spec, method, bins)

    timestep_days = _timestep_days(ds, dim)
    native_interval = _coverage_cell(ds)
    present = _present(ds, dim)
    resampler = ds.resample({dim: freq})
    n = ds.sizes[dim]
    chunks, labels, coverages = [], [], []
    for label, group in resampler.groups.items():
        indices = _group_indices(group, n)
        if not indices:
            continue
        expected = _expected_from_interval(spec, label, native_interval, timestep_days)
        chunks.append(_reduce(ds.isel({dim: indices}), method=method, dim=dim))
        labels.append(np.datetime64(label) if not hasattr(label, "calendar") else label)
        coverages.append(_coverage_fraction(float(present[indices].sum()), expected))
    if not chunks:
        raise UsageError(_empty_bins_message(spec, dim))
    out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})
    return _assign_coverage(out, dim, coverages)


def _aggregate_time_anchored(ds, dim, spec, method, end_time, *, start_time=None):
    """Backward fixed-width bins whose last right edge is ``end_time``.

    Same geometry as forecast ``step`` buckets: half-open ``[left, right)``,
    labeled at the left edge. Walks while the bin's right edge is still after
    the effective data start, then keeps bins that contain samples and stamps
    ``aggregation_coverage``. Optional ``start_time`` raises the earliest
    coverage floor.
    """
    import numpy as np
    import xarray as xr

    period_days = spec["anchor_days"]
    raw = np.asarray(ds[dim].values)
    if raw.size == 0:
        raise UsageError(_empty_bins_message(spec, dim))
    if raw.dtype.kind == "O" and hasattr(raw.flat[0], "calendar"):
        import cftime

        calendar = raw.flat[0].calendar
        bin_width = _dt.timedelta(days=period_days)
        data_min = raw.min()
        if start_time is not None:
            try:
                start_cf = cftime.datetime(
                    start_time.year, start_time.month, start_time.day, calendar=calendar
                )
            except ValueError:
                raise UsageError(
                    f"--start-time {start_time.isoformat()} invalid in {calendar!r}"
                ) from None
            if start_cf > data_min:
                data_min = start_cf
        try:
            right = cftime.datetime(end_time.year, end_time.month, end_time.day, calendar=calendar)
        except ValueError:
            raise UsageError(f"--end-time {end_time.isoformat()} invalid in {calendar!r}") from None
        label = lambda edge: edge  # noqa: E731
    else:
        import pandas as pd

        bin_width = pd.Timedelta(days=period_days)
        data_min = pd.Timestamp(pd.to_datetime(raw).min())
        if start_time is not None:
            start_ts = pd.Timestamp(start_time)
            if start_ts > data_min:
                data_min = start_ts
        right = pd.Timestamp(end_time)
        label = lambda edge: np.datetime64(edge)  # noqa: E731

    timestep_days = _timestep_days(ds, dim)
    native_interval = _coverage_cell(ds)
    present = _present(ds, dim)
    bins, r = [], right
    # Stop once the right edge is at/before the first sample — do not require
    # left >= data_min, or a bin that still holds data is never created.
    while r > data_min:
        left = r - bin_width
        bins.append((left, r))
        r = left
    bins.reverse()
    if _cf_bounds(ds, dim) is not None:
        bound_bins = [(left, right, label(left)) for left, right in bins]
        return _aggregate_from_bounds(ds, dim, spec, method, bound_bins)
    chunks, labels, coverages = [], [], []
    for left, right in bins:
        keep = np.nonzero((raw >= left) & (raw < right))[0]
        if keep.size == 0:
            continue
        expected = _expected_from_interval(spec, left, native_interval, timestep_days)
        chunks.append(_reduce(ds.isel({dim: keep}), method=method, dim=dim))
        labels.append(label(left))
        coverages.append(_coverage_fraction(float(present[keep].sum()), expected))
    if not chunks:
        raise UsageError(_empty_bins_message(spec, dim))
    out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})
    return _assign_coverage(out, dim, coverages)


def _aggregate_step(ds, spec, method):
    import numpy as np
    import pandas as pd
    import xarray as xr

    if spec["key"] == "monthly":
        raise UsageError("monthly aggregation is not defined for step")
    window = pd.Timedelta(days=spec["anchor_days"]).to_timedelta64()
    steps = ds["step"].values
    bounds = _cf_bounds(ds, "step")
    max_step = bounds[:, 1].max() if bounds is not None else steps.max()
    edges = np.arange(0, max_step + window, window, dtype=steps.dtype)
    if bounds is not None:
        bins = []
        for left in edges:
            right = left + window
            bins.append((left, right, left))
        return _aggregate_from_bounds(ds, "step", spec, method, bins)
    native_interval = _coverage_cell(ds)
    timestep_days = _timestep_days(ds, "step")
    present = _present(ds, "step")
    chunks, labels, coverages = [], [], []
    for left in edges:
        right = left + window
        mask = (steps >= left) & (steps < right)
        if not mask.any():
            continue
        n_present = float(present[mask].sum())
        expected = _expected_from_interval(spec, right, native_interval, timestep_days)
        chunks.append(_reduce(ds.isel(step=np.where(mask)[0]), method=method, dim="step"))
        labels.append(left)
        coverages.append(_coverage_fraction(n_present, expected))
    if not chunks:
        raise UsageError(_empty_bins_message(spec, "step"))
    out = xr.concat(chunks, dim="step").assign_coords(step=labels)
    return _assign_coverage(out, "step", coverages)


def _stamp_attrs(out, dim, agg_period, method, interval, *, data_interval=None):
    cf_method = _METHOD_TO_CF[method]
    cm = format_cell_methods(dim, cf_method, interval=interval)
    for name in out.data_vars:
        attrs = {**out[name].attrs, AGGREGATION_PERIOD_ATTR: agg_period, "cell_methods": cm}
        native = data_interval or attrs.get(DATA_INTERVAL_ATTR)
        if native:
            attrs[DATA_INTERVAL_ATTR] = native
        out[name].attrs = attrs
    return out


def _rolling_aggregation_period(ds, dim, window, interval):
    """Pint duration for a rolling window of ``window`` input steps."""
    if interval is not None:
        try:
            from weather_skills_core.units import parse_aggregation_period

            step = parse_aggregation_period(interval)
            return format_duration(step * window)
        except UsageError:
            pass
    return f"{window} day"


@weather_skill(
    name="aggregate-temporal",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "-i",
    "--input",
    type=Dataset(["time", "prediction_timedelta"]),
    required=True,
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--period",
    help=(
        "Calendar/step window: daily, weekly (7 days), dekadal (a fixed 10 days, "
        "not calendar dekads 1-10/11-20/21-end), monthly (calendar months), or "
        "a pint duration (e.g. '21 day', or sub-daily '6 hour'). Mutex with --window."
    ),
)
@weather_skill.argument(
    "--window",
    type=int,
    help="Rolling window in axis steps (mutex with --period).",
)
@weather_skill.argument("--align", default="left", choices=["left", "right", "center"])
@weather_skill.argument(
    "--stride",
    help="With --window: int step or stride_dates string (day/week/Monday/...).",
)
@weather_skill.argument("--method", default="mean", choices=["mean", "max", "min"])
@weather_skill.argument("--time-dim")
@weather_skill.argument(
    "--end-time",
    help=(
        "With --period: exclusive right edge of the final bin (bins walk "
        "backward; labeled at the left edge, same as forecast step buckets)."
    ),
)
@weather_skill.argument(
    "--start-time",
    help="With --period and --end-time: optional earliest coverage floor.",
)
def aggregate(
    ds,
    variable,
    time_dim,
    period,
    window,
    align,
    stride,
    method,
    end_time,
    start_time,
    **kwargs,
):
    """Roll up a Zarr along its time or forecast-step axis into fixed or rolling windows (mean, min, or max)."""
    import cf_xarray  # noqa: F401

    if (period is None) == (window is None):
        raise UsageError("exactly one of --period or --window is required")
    if window is not None and (end_time is not None or start_time is not None):
        raise UsageError("--start-time/--end-time cannot be combined with --window")
    if start_time is not None and end_time is None:
        raise UsageError("--start-time requires --end-time")
    if window is None and stride is not None:
        raise UsageError("--stride requires --window")
    if window is None and align != "left":
        raise UsageError("--align requires --window")

    if variable is not None:
        ds = ds[list(dict.fromkeys(variable))]

    if time_dim:
        dim = time_dim
    else:
        try:
            cf_time = ds.cf["time"].name
        except KeyError:
            cf_time = "time" if "time" in ds.dims else None
        if cf_time is not None and cf_time in ds.dims:
            dim = "step" if ds.sizes[cf_time] == 1 and "step" in ds.dims else cf_time
        elif "step" in ds.dims:
            dim = "step"
        else:
            raise UsageError(f"no time/step dim in {list(ds.dims)}; pass --time-dim")

    has_bounds = _cf_bounds(ds, dim) is not None
    if not has_bounds:
        try:
            ds = stamp_data_interval(ds, dim=dim)
        except UsageError:
            pass
        has_bounds = _cf_bounds(ds, dim) is not None
    native_interval = None if has_bounds else _native_interval(ds, dim)
    interval = native_interval
    if not has_bounds:
        try:
            if interval is None:
                interval = format_duration(infer_timestep(ds, dim))
        except UsageError:
            pass

    stride_val = stride
    if stride is not None:
        try:
            stride_val = int(stride)
        except ValueError:
            pass

    if window is not None:
        if has_bounds:
            raise UsageError(
                "--window is a step count; irregular axes with CF bounds require --period"
            )
        if interval is not None and ds.sizes[dim] > 1:
            import numpy as np
            import pandas as pd

            spacing = pd.to_timedelta(np.diff(np.asarray(ds[dim].values)))
            native = pd.Timedelta(
                seconds=float(parse_aggregation_period(interval).to("s").magnitude)
            )
            if (spacing != native).any():
                # A window of N rows across a gap spans more than N intervals.
                raise UsageError(
                    f"--window counts rows and {dim!r} has missing rows (spacing other "
                    f"than {interval}); use --period, which counts them against coverage"
                )
        try:
            out = roll_and_agg(ds, window, dim, method, align=align, stride=stride_val)
        except KeyError:
            # A calendar stride (month, Monday) picks dates; on a weekly or
            # monthly axis most of them are not labels, so there is nothing to pick.
            raise UsageError(
                f"--stride {stride} picks dates that are not on the {dim!r} axis "
                f"(spacing {interval}); use an integer --stride (e.g. 4 for every "
                "4th window), or --period for calendar bins"
            ) from None
        n = out.sizes.get(dim, 0)
        if n:
            import numpy as np
            import xarray as xr

            present = xr.DataArray(
                _finite_along(ds, dim).astype("float64"),
                dims=[dim],
                coords={dim: ds[dim]},
            )
            if window == 1 and stride is None:
                cov = present
            else:
                cov = roll_and_agg(
                    present.to_dataset(name="_c"),
                    window,
                    dim,
                    "mean",
                    align=align,
                    stride=stride_val,
                )["_c"]
            out = _assign_coverage(out, dim, np.asarray(cov.values, dtype=float))
        agg_period = _rolling_aggregation_period(ds, dim, window, interval)
        return _stamp_attrs(out, dim, agg_period, method, interval, data_interval=native_interval)

    spec = _resolve_period(period)
    if native_interval and _durations_equal(spec["agg"], native_interval):
        import numpy as np

        out = ds
        n = out.sizes.get(dim, 0)
        if n:
            out = _assign_coverage(out, dim, np.ones(n, dtype=float))
        return _stamp_attrs(out, dim, spec["agg"], method, interval, data_interval=native_interval)
    # Each input sample covers [t, t + cell). Bins must hold whole cells, or a
    # weekly mean lands entirely in the month of its label and still counts as
    # complete coverage.
    if has_bounds and spec["key"] != "monthly":
        bounds = _cf_bounds(ds, dim)
        widest = float((_as_ns(bounds[:, 1]) - _as_ns(bounds[:, 0])).max()) / 86400e9
        if spec["anchor_days"] < widest - 1e-9:
            # A bin shorter than a sample would spread that sample's mean over it.
            raise UsageError(
                f"--period {period} is shorter than the widest {dim} interval in the input "
                f"({widest * 24:g} hour); "
                "aggregate to that interval or longer"
            )
    cell = None if has_bounds else _coverage_cell(ds)
    if cell:
        cell_days = float(parse_aggregation_period(cell).to("day").magnitude)
        if spec["key"] == "monthly":
            fits = cell_days <= 1
        else:
            ratio = spec["anchor_days"] / cell_days
            fits = ratio >= 1 and abs(ratio - round(ratio)) < 1e-6
        if not fits:
            raise UsageError(
                f"--period {period} bins cannot hold whole {cell} input samples; aggregate "
                f"from data at a spacing that divides {spec['agg']} (e.g. the daily input)"
            )
        raw = ds[dim].values
        if end_time is not None and cell_days > 1 and raw.dtype.kind == "M":
            import numpy as np

            offset = (np.datetime64(end_time, "ns") - raw[0]) / np.timedelta64(1, "D")
            if abs(offset / cell_days - round(offset / cell_days)) > 1e-6:
                raise UsageError(
                    f"--end-time {end_time} is not on the {cell} grid of the input "
                    f"(first sample {str(raw[0])[:10]}), so bins would split input samples"
                )
    if dim == "step":
        out = _aggregate_step(ds, spec, method)
        if end_time is not None or start_time is not None:
            print(
                "--start-time/--end-time have no effect on step aggregation",
                file=sys.stderr,
            )
    elif end_time is not None:
        if spec["key"] == "monthly":
            raise UsageError(
                "--end-time anchors fixed-width bins; monthly bins are already "
                "calendar months (1st to 1st). Drop --end-time, or use a whole-day "
                "--period such as '30 day'."
            )
        import numpy as np

        later = 0
        if ds[dim].dtype.kind == "M":
            later = int((ds[dim].values >= np.datetime64(end_time, "ns")).sum())
        if later:
            print(
                f"Note: {later} sample(s) at or after --end-time {end_time} are not aggregated "
                "(--end-time is exclusive: pass the day after the last day to include).",
                file=sys.stderr,
            )
        out = _aggregate_time_anchored(
            ds,
            dim,
            spec,
            method,
            end_time,
            start_time=start_time,
        )
    else:
        out = _aggregate_time_resample(ds, dim, spec, method)

    # Incomplete bins are kept with their coverage; convert-to-totals drops them
    # at its default --min-coverage 1.0. Say so now, not three steps later.
    import numpy as np
    import pandas as pd

    cov = np.asarray(out[AGGREGATION_COVERAGE_COORD].values, dtype=float)
    low = cov < 1.0 - 1e-12
    if low.any():
        labels = pd.Index(np.asarray(out[dim].values)[low])
        listed = ", ".join(
            f"{lab} ({c:.3g})" for lab, c in zip(labels[:10], cov[low][:10], strict=True)
        )
        listed += ", ..." if low.sum() > 10 else ""
        print(
            f"Warning: {int(low.sum())} of {cov.size} {spec['key']} bin(s) are "
            f"incomplete (aggregation_coverage < 1): {listed}. convert-to-totals "
            "drops them at its default --min-coverage 1.0.",
            file=sys.stderr,
        )
        # Interior partial bins of a back-to-back resample mean the input has gaps.
        if dim != "step" and end_time is None and low[1:-1].any():
            print(
                f"Bins run back to back from {pd.Index(out[dim].values)[0]}, so a "
                "series with gaps (e.g. one window per year after concat) straddles "
                "bins. Aggregate each segment before concat.",
                file=sys.stderr,
            )

    return _stamp_attrs(out, dim, spec["agg"], method, interval, data_interval=spec["agg"])


if __name__ == "__main__":
    aggregate()

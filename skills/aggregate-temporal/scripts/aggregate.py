# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.2",
#   "pandas",
# ]
# ///
"""Roll up a Zarr along its time or forecast-step axis into fixed or rolling windows (mean, min, or max)."""

import datetime as _dt
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import stride_dates
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
_SKILL_VERSION = "0.0.6"

# Named --period / --window → (aggregation_period stamped, resample freq, length in days).
NAMED_PERIODS = {
    "daily": ("1 day", "1D", 1),
    "weekly": ("7 day", "7D", 7),
    "dekadal": ("1 dekad", "10D", 10),
    "monthly": ("1 month", "MS", 30),
}
_METHOD_TO_CF = {"mean": "mean", "max": "maximum", "min": "minimum"}


def _as_ns(val):
    """Times or durations (numpy, pandas, or cftime) as int64 nanoseconds."""
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
        "a duration (e.g. '21 day', or sub-daily '6 hour'). Mutex with --window."
    ),
)
@weather_skill.argument(
    "--window",
    help=(
        "Rolling window length: daily, weekly, dekadal, or a duration (e.g. '7 day', "
        "'12 hour'); one window per sample. Mutex with --period."
    ),
)
@weather_skill.argument("--align", default="left", choices=["left", "right", "center"])
@weather_skill.argument(
    "--stride",
    help="With --window: keep every Nth window (int), or day/week/month/year/weekday dates.",
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
    import numpy as np
    import pandas as pd
    import xarray as xr

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
    # dequantify_dataset keeps the standard spelling (mm day-1, not millimeter / day).
    ds = dequantify_dataset(ds)

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

    # Each input sample's cell: CF bounds (uneven spacing) or a scalar data_interval.
    try:
        ds = stamp_data_interval(ds, dim=dim)
    except UsageError:
        pass
    bounds_name = ds[dim].attrs.get("bounds")
    if not (isinstance(bounds_name, str) and bounds_name in ds):
        bounds_name = f"{dim}_bounds" if f"{dim}_bounds" in ds else None
    bounds = native_interval = cell = None
    if bounds_name is not None:
        bounds = np.asarray(ds[bounds_name].values)
        if bounds.ndim != 2 or bounds.shape[-1] != 2:
            raise UsageError(f"{bounds_name!r} must have shape ({dim}, 2) start/end")
        starts, ends = _as_ns(bounds[:, 0]), _as_ns(bounds[:, 1])
        ds = ds.drop_vars([bounds_name, f"{dim}_bounds"], errors="ignore")
        ds[dim].attrs = {k: v for k, v in ds[dim].attrs.items() if k != "bounds"}
    else:
        native_interval = data_interval_of(ds)
        if native_interval is None:
            try:
                native_interval = format_duration(infer_timestep(ds, dim))
            except UsageError:
                pass
        # Coverage counts input samples: a re-aggregated input's are its aggregation_period.
        for name in ds.data_vars:
            val = ds[name].attrs.get(AGGREGATION_PERIOD_ATTR)
            if isinstance(val, str) and val.strip():
                cell = val.strip()
                break
        else:
            cell = data_interval_of(ds)

    key = period or window
    flag = "--period" if period else "--window"
    if key in NAMED_PERIODS:
        agg, freq, length_days = NAMED_PERIODS[key]
    else:
        try:
            quantity = parse_aggregation_period(key)
        except UsageError as exc:
            raise UsageError(
                f"{flag} {key!r} must be one of {', '.join(NAMED_PERIODS)} "
                f"or a pint duration (e.g. '21 day', '6 hour'): {exc}"
            ) from None
        length_days = float(quantity.to("day").magnitude)
        seconds = length_days * 86400
        if length_days <= 0 or abs(seconds - round(seconds)) > 1e-6:
            raise UsageError(
                f"{flag} {key!r} must be a named period "
                f"({', '.join(NAMED_PERIODS)}) or a positive whole number of seconds "
                "(e.g. '21 day', '6 hour')"
            )
        agg = format_duration(quantity)
        if abs(length_days - round(length_days)) < 1e-6:
            length_days = int(round(length_days))
            freq = f"{length_days}D"
        else:
            # Sub-daily: bins of this many seconds, aligned to 00 UTC of the first day.
            freq = f"{int(round(seconds))}s"
    monthly = key == "monthly"
    if window is not None and monthly:
        raise UsageError("--window takes a fixed length such as '30 day'; months vary in length")

    # --period at the input's own spacing: nothing to aggregate, just stamp it.
    same = False
    if window is None and native_interval:
        try:
            same = parse_aggregation_period(agg) == parse_aggregation_period(native_interval)
        except UsageError:
            pass

    # Each input sample covers [t, t + cell). Bins must hold whole cells, or a
    # weekly mean lands entirely in the month of its label and still counts as
    # complete coverage.
    if bounds is not None and not monthly:
        widest = float((ends - starts).max()) / 86400e9
        if length_days < widest - 1e-9:
            # A bin shorter than a sample would spread that sample's mean over it.
            raise UsageError(
                f"{key!r} is shorter than the widest {dim} interval in the input "
                f"({widest * 24:g} hour); "
                "aggregate to that interval or longer"
            )
    if cell and not same:
        cell_days = float(parse_aggregation_period(cell).to("day").magnitude)
        if monthly:
            fits = cell_days <= 1
        else:
            ratio = length_days / cell_days
            fits = ratio >= 1 and abs(ratio - round(ratio)) < 1e-6
        if not fits:
            raise UsageError(
                f"{key!r} windows cannot hold whole {cell} input samples; aggregate "
                f"from data at a spacing that divides {agg} (e.g. the daily input)"
            )
        raw = ds[dim].values
        if end_time is not None and cell_days > 1 and raw.dtype.kind == "M":
            offset = (np.datetime64(end_time, "ns") - raw[0]) / np.timedelta64(1, "D")
            if abs(offset / cell_days - round(offset / cell_days)) > 1e-6:
                raise UsageError(
                    f"--end-time {end_time} is not on the {cell} grid of the input "
                    f"(first sample {str(raw[0])[:10]}), so bins would split input samples"
                )

    # Every mode below builds half-open [left, right) bins with a label each.
    raw = np.asarray(ds[dim].values)
    width = _dt.timedelta(seconds=round(length_days * 86400))
    bins = []
    if same:
        pass
    elif window is not None:
        # One window per sample, labeled at that sample. --align puts the sample at
        # the window's start (left), middle (center), or end (right).
        try:
            stride = int(stride)
        except (TypeError, ValueError):
            pass
        anchors = np.arange(raw.size)
        if isinstance(stride, int):
            if stride < 1:
                raise UsageError(f"--stride must be >= 1; got {stride}")
            anchors = anchors[::stride]
        elif stride is not None:
            if raw.dtype.kind != "M":
                raise UsageError(
                    "a date --stride (day, week, Monday, ...) needs a datetime time axis"
                )
            anchors = anchors[np.isin(raw, stride_dates(raw[0], raw[-1], stride=stride))]
            if anchors.size == 0:
                raise UsageError(
                    f"--stride {stride} picks no dates on the {dim!r} axis; use an integer --stride"
                )
        times = _as_ns(raw)
        if bounds is None:
            cell_ns = 0
            if native_interval is not None:
                cell_ns = round(float(parse_aggregation_period(native_interval).to("ns").magnitude))
            starts, ends = times, times + cell_ns
        width_ns = int(round(length_days * 86400e9))
        cells = np.maximum(ends - starts, 1)
        if align == "left":
            window_starts = starts
        elif align == "right":
            window_starts = ends - width_ns
        else:
            # Centre on the sample, rounded back to a whole cell for even counts.
            window_starts = starts - (width_ns // cells - 1) // 2 * cells
        for i in anchors:
            offset_ns = int(window_starts[i] - times[i])
            if raw.dtype.kind in "Mm":
                left = raw[i] + np.timedelta64(offset_ns, "ns")
                right = left + np.timedelta64(width_ns, "ns")
            else:
                # cftime dates add datetime.timedelta.
                left = raw[i] + _dt.timedelta(microseconds=offset_ns // 1000)
                right = left + _dt.timedelta(microseconds=width_ns // 1000)
            bins.append((left, right, raw[i]))
    elif dim == "step":
        if monthly:
            raise UsageError("monthly aggregation is not defined for step")
        if end_time is not None or start_time is not None:
            print("--start-time/--end-time have no effect on step aggregation", file=sys.stderr)
        step = pd.Timedelta(width).to_timedelta64()
        max_step = (bounds[:, 1] if bounds is not None else raw).max()
        for left in np.arange(0, max_step + step, step, dtype=raw.dtype):
            bins.append((left, left + step, left))
    elif end_time is not None:
        # Backward fixed-width bins whose last right edge is --end-time, labeled at
        # the left edge like forecast step buckets.
        if monthly:
            raise UsageError(
                "--end-time anchors fixed-width bins; monthly bins are already "
                "calendar months (1st to 1st). Drop --end-time, or use a whole-day "
                "--period such as '30 day'."
            )
        if raw.size == 0:
            raise UsageError(f"no {key} bins contained samples on {dim}")
        later = 0
        if raw.dtype.kind == "M":
            later = int((raw >= np.datetime64(end_time, "ns")).sum())
        if later:
            print(
                f"Note: {later} sample(s) at or after --end-time {end_time} are not aggregated "
                "(--end-time is exclusive: pass the day after the last day to include).",
                file=sys.stderr,
            )
        if raw.dtype.kind == "O" and hasattr(raw.flat[0], "calendar"):
            import cftime

            floor = raw.min()
            calendar = raw.flat[0].calendar
            if start_time is not None:
                try:
                    start = cftime.datetime(
                        start_time.year, start_time.month, start_time.day, calendar=calendar
                    )
                except ValueError:
                    raise UsageError(
                        f"--start-time {start_time.isoformat()} invalid in {calendar!r}"
                    ) from None
                floor = max(floor, start)
            try:
                right = cftime.datetime(
                    end_time.year, end_time.month, end_time.day, calendar=calendar
                )
            except ValueError:
                raise UsageError(
                    f"--end-time {end_time.isoformat()} invalid in {calendar!r}"
                ) from None
        else:
            floor = pd.to_datetime(raw).min()
            if start_time is not None:
                floor = max(floor, pd.Timestamp(start_time))
            right = pd.Timestamp(end_time)
        # Stop once the right edge is at/before the first sample — do not require
        # left >= floor, or a bin that still holds data is never created.
        while right > floor:
            bins.insert(0, (right - width, right, right - width))
            right -= width
    else:
        # Forward calendar bins from the first sample.
        for label in ds.resample({dim: freq}).groups:
            if not hasattr(label, "calendar"):
                label = pd.Timestamp(label)
            if monthly:
                # replace() works for pandas and cftime labels alike.
                right = label.replace(
                    year=label.year + label.month // 12, month=label.month % 12 + 1
                )
            else:
                right = label + width
            bins.append((label, right, label))

    if same:
        # Keep the input's own coverage: a 3/7-complete week stays 3/7 complete.
        out = ds
        coverages = np.ones(ds.sizes[dim])
        if AGGREGATION_COVERAGE_COORD in ds.coords and ds[AGGREGATION_COVERAGE_COORD].dims == (
            dim,
        ):
            coverages = ds[AGGREGATION_COVERAGE_COORD].values
    else:
        # A sample is present when every variable has some finite value at it: a
        # persistent spatial hole (land mask) is not missing, an unpublished forecast
        # lead that is NaN everywhere is.
        present = np.ones(raw.size)
        for name in ds.data_vars:
            da = ds[name]
            if dim in da.dims:
                present *= np.isfinite(da).any([d for d in da.dims if d != dim]).values
        # Re-aggregating, a 3/7-complete week counts as 3/7 of a week, not a whole one.
        if bounds is None and AGGREGATION_COVERAGE_COORD in ds.coords:
            if ds[AGGREGATION_COVERAGE_COORD].dims == (dim,):
                present *= np.nan_to_num(ds[AGGREGATION_COVERAGE_COORD].values.astype(float))
        if bounds is None:
            timestep_days = 1.0
            try:
                timestep_days = float(infer_timestep(ds, dim).to("day").magnitude)
            except UsageError:
                pass

        chunks, labels, coverages = [], [], []
        for left, right, label in bins:
            if bounds is not None:
                # Weight each sample by how much of its cell falls in the bin.
                left_ns, right_ns = int(_as_ns(left)), int(_as_ns(right))
                overlap = np.maximum(np.minimum(ends, right_ns) - np.maximum(starts, left_ns), 0)
                idx = np.flatnonzero(overlap > 0)
                if idx.size == 0 or right_ns <= left_ns:
                    continue
                coverage = float((overlap * present)[idx].sum()) / (right_ns - left_ns)
            else:
                idx = np.flatnonzero((raw >= left) & (raw < right))
                if idx.size == 0:
                    continue
                days, bin_period = length_days, agg
                if monthly:
                    # Month length in the label's own calendar (noleap Feb is 28, 360_day is 30).
                    days = left.daysinmonth if hasattr(left, "calendar") else left.days_in_month
                    bin_period = f"{days} day"
                expected = max(1, round(days / timestep_days))
                if cell:
                    try:
                        expected = expected_samples_in_period(bin_period, cell)
                    except UsageError:
                        pass
                coverage = float(present[idx].sum()) / expected
            sub = ds.isel({dim: idx})
            if bounds is not None and method == "mean":
                weights = xr.DataArray(
                    overlap[idx].astype(float), dims=[dim], coords={dim: sub[dim].values}
                )
                # Weight only finite samples. Dividing by sum(weights) treats NaN as 0, which
                # turns unpublished GEFS long leads (all-NaN weeks 4–5) into fake dry zeros.
                count = (sub.notnull() * weights).sum(dim=dim)
                reduced = (sub * weights).sum(dim=dim, skipna=True, keep_attrs=True) / count
            else:
                count = sub.notnull().sum(dim=dim)
                reduced = getattr(sub, method)(dim=dim, keep_attrs=True)
            # A cell missing samples other cells have is NaN, not a mean of what it has:
            # convert-to-totals would scale that mean up to the full period.
            chunks.append(reduced.where(count >= count.max()))
            labels.append(label)
            coverages.append(min(1.0, coverage))
        if not chunks:
            raise UsageError(f"no {key} bins contained samples on {dim}")
        out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})

    out = out.assign_coords({AGGREGATION_COVERAGE_COORD: (dim, np.asarray(coverages, dtype=float))})
    out[AGGREGATION_COVERAGE_COORD].attrs.update(
        long_name="fraction of native samples with finite data in the interval",
        units="1",
        valid_min=0.0,
        valid_max=1.0,
    )

    # Incomplete bins are kept with their coverage; convert-to-totals drops them
    # at its default --min-coverage 1.0. Say so now, not three steps later.
    cov = np.asarray(out[AGGREGATION_COVERAGE_COORD].values, dtype=float)
    low = cov < 1.0 - 1e-12
    if low.any():
        low_labels = pd.Index(np.asarray(out[dim].values)[low])
        listed = ", ".join(
            f"{lab} ({c:.3g})" for lab, c in zip(low_labels[:10], cov[low][:10], strict=True)
        )
        listed += ", ..." if low.sum() > 10 else ""
        print(
            f"Warning: {int(low.sum())} of {cov.size} {key} bin(s) are "
            f"incomplete (aggregation_coverage < 1): {listed}. convert-to-totals "
            "drops them at its default --min-coverage 1.0.",
            file=sys.stderr,
        )
        # Interior partial bins of a back-to-back resample mean the input has gaps.
        if window is None and dim != "step" and end_time is None and low[1:-1].any():
            print(
                f"Bins run back to back from {pd.Index(out[dim].values)[0]}, so a "
                "series with gaps (e.g. one window per year after concat) straddles "
                "bins. Aggregate each segment before concat.",
                file=sys.stderr,
            )

    # Rolling windows overlap, one per sample, so the output keeps the input spacing.
    data_interval = native_interval if window is not None or same else agg
    cell_methods = format_cell_methods(dim, _METHOD_TO_CF[method], interval=native_interval)
    for name in out.data_vars:
        attrs = {**out[name].attrs, AGGREGATION_PERIOD_ATTR: agg, "cell_methods": cell_methods}
        if data_interval:
            attrs[DATA_INTERVAL_ATTR] = data_interval
        out[name].attrs = attrs
    return out


if __name__ == "__main__":
    aggregate()

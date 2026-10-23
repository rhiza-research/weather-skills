# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/weather-skills/weather-skills-core@per-cell-coverage",
#   "pandas",
# ]
# ///
"""Roll up a Zarr along its time or forecast-step axis into fixed or rolling windows (mean, min, or max)."""

import datetime as _dt
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import measured_variables
from weather_skills_core.standard_utils import roll_and_agg
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    COVERAGE_SUFFIX,
    DATA_INTERVAL_ATTR,
    coverage_values,
    coverage_variable,
    data_interval_of,
    dequantify_dataset,
    format_cell_methods,
    format_duration,
    infer_timestep,
    parse_aggregation_period,
    stamp_data_interval,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

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


def _empty_bins_message(spec, dim):
    return f"no {spec['key']} bins contained samples on {dim}"


def _assign_coverage(out, dim, coverages):
    import numpy as np

    if out.sizes.get(dim, 0) == 0:
        return out
    values = np.asarray(coverages, dtype=float)
    out = out.assign_coords({AGGREGATION_COVERAGE_COORD: (dim, values)})
    out[AGGREGATION_COVERAGE_COORD].attrs.update(
        long_name="largest share of the interval with data at any cell",
        units="1",
        valid_min=0.0,
        valid_max=1.0,
    )
    return out


def _coverage_attrs(name):
    return {
        "long_name": f"share of the interval with {name} data",
        "units": "1",
        "valid_min": 0.0,
        "valid_max": 1.0,
    }


def _link_coverage(attrs, name):
    """``attrs`` with ``<name>_coverage`` listed in ``ancillary_variables``."""
    listed = str(attrs.get("ancillary_variables", "")).split()
    cov = f"{name}{COVERAGE_SUFFIX}"
    return {**attrs, "ancillary_variables": " ".join(dict.fromkeys([*listed, cov]))}


def _bins(ds, dim, spec, end_time=None, start_time=None):
    """The ``(left, right, label)`` bins of ``--period`` on ``dim``, labeled at the left edge.

    ``step``: buckets from lead 0. ``end_time``: fixed-width bins walking back from it,
    while the right edge is after the data start (or ``start_time``). Otherwise calendar
    resample bins (months run 1st to 1st).
    """
    import numpy as np
    import pandas as pd

    raw = np.asarray(ds[dim].values)
    if raw.size == 0:
        raise UsageError(_empty_bins_message(spec, dim))
    if dim == "step":
        if spec["key"] == "monthly":
            raise UsageError("monthly aggregation is not defined for step")
        window = pd.Timedelta(days=spec["anchor_days"]).to_timedelta64()
        bounds = _cf_bounds(ds, "step")
        max_step = bounds[:, 1].max() if bounds is not None else raw.max()
        edges = np.arange(0, max_step + window, window, dtype=raw.dtype)
        return [(left, left + window, left) for left in edges]
    if end_time is not None:
        if raw.dtype.kind == "O" and hasattr(raw.flat[0], "calendar"):
            import cftime

            calendar = raw.flat[0].calendar
            bin_width = _dt.timedelta(days=spec["anchor_days"])
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
                data_min = max(data_min, start_cf)
            try:
                right = cftime.datetime(
                    end_time.year, end_time.month, end_time.day, calendar=calendar
                )
            except ValueError:
                raise UsageError(
                    f"--end-time {end_time.isoformat()} invalid in {calendar!r}"
                ) from None
            label = lambda edge: edge  # noqa: E731
        else:
            bin_width = pd.Timedelta(days=spec["anchor_days"])
            data_min = pd.Timestamp(pd.to_datetime(raw).min())
            if start_time is not None:
                data_min = max(data_min, pd.Timestamp(start_time))
            right = pd.Timestamp(end_time)
            label = lambda edge: np.datetime64(edge)  # noqa: E731
        bins = []
        # Stop once the right edge is at/before the first sample — do not require
        # left >= data_min, or a bin that still holds data is never created.
        while right > data_min:
            bins.append((right - bin_width, right, label(right - bin_width)))
            right = right - bin_width
        return bins[::-1]
    bins = []
    for label in ds.resample({dim: spec["freq"]}).groups:
        if not hasattr(label, "calendar"):
            label = pd.Timestamp(label)
        if spec["key"] == "monthly":
            # replace() works for pandas and cftime labels alike.
            year, month = (
                (label.year + 1, 1) if label.month == 12 else (label.year, label.month + 1)
            )
            left, right = label, label.replace(year=year, month=month)
        elif hasattr(label, "calendar"):
            left, right = label, label + _dt.timedelta(days=spec["anchor_days"])
        else:
            left = np.datetime64(label)
            right = left + np.timedelta64(int(round(spec["anchor_days"] * 86400)), "s")
        bins.append((left, right, left if hasattr(label, "calendar") else np.datetime64(label)))
    return bins


def _reduce_bins(ds, dim, spec, method, bins):
    """Reduce every bin cell by cell, and stamp each cell's coverage.

    Each sample spans its CF bounds, else ``[t, t + cell)``, and is weighted by its
    overlap with the bin. Per cell, ``<var>_coverage`` is the overlap with data (times
    the input's own coverage when re-aggregating) over the bin length; the value is the
    overlap-weighted mean (or max/min) of the finite samples. A cell with less data than
    the bin's best-covered cell is NaN: convert-to-totals would scale its mean up to the
    whole bin. The bin's ``aggregation_coverage`` is its best cell's coverage.
    """
    import numpy as np
    import xarray as xr

    bounds = _cf_bounds(ds, dim)
    if bounds is not None:
        starts, ends = _as_ns(bounds[:, 0]), _as_ns(bounds[:, 1])
    else:
        starts = _as_ns(ds[dim].values)
        cell = _coverage_cell(ds)
        try:
            width = parse_aggregation_period(cell) if cell else infer_timestep(ds, dim)
        except UsageError:
            width = parse_aggregation_period("1 day")
        ends = starts + int(round(float(width.to("second").magnitude) * 1e9))
    work = dequantify_dataset(_drop_bounds(ds, dim))
    measured = [name for name in measured_variables(work) if dim in work[name].dims]
    # Input coverage when re-aggregating: per cell if stamped, else per interval.
    step_share = coverage_values(work, dim)
    chunks, labels, bin_coverage = [], [], []
    for left, right, label in bins:
        left_ns = int(np.asarray(_as_ns(left)).reshape(-1)[0])
        right_ns = int(np.asarray(_as_ns(right)).reshape(-1)[0])
        overlap = np.maximum(np.minimum(ends, right_ns) - np.maximum(starts, left_ns), 0)
        idx = np.flatnonzero(overlap > 0)
        if right_ns <= left_ns or idx.size == 0:
            continue
        sub = work.isel({dim: idx})
        w = xr.DataArray(overlap[idx].astype(float), dims=[dim])
        chunk = {name: sub[name] for name in work.data_vars if dim not in work[name].dims}
        best_by_var = []
        for name in measured:
            da = sub[name]
            finite = da.notnull()
            share = coverage_variable(sub, name)
            share = xr.DataArray(step_share[idx], dims=[dim]) if share is None else share.fillna(0)
            coverage = ((w * finite * share).sum(dim) / (right_ns - left_ns)).clip(max=1.0)
            if method == "mean":
                value = (da * w).sum(dim, skipna=True) / (w * finite).sum(dim)
            else:
                value = getattr(da, method)(dim)
            best = float(coverage.max()) if coverage.size else 0.0
            value = value.where(coverage >= best - 1e-9)
            if da.dtype.kind == "f":
                value = value.astype(da.dtype)
            chunk[name] = value.assign_attrs(_link_coverage(da.attrs, name))
            chunk[f"{name}{COVERAGE_SUFFIX}"] = coverage.astype("float32").assign_attrs(
                _coverage_attrs(name)
            )
            best_by_var.append(best)
        chunks.append(xr.Dataset(chunk))
        labels.append(label)
        bin_coverage.append(min(best_by_var, default=1.0))
    if not chunks:
        raise UsageError(_empty_bins_message(spec, dim))
    out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})
    return _assign_coverage(out, dim, bin_coverage)


def _stamp_attrs(out, dim, agg_period, method, interval, *, data_interval=None):
    cf_method = _METHOD_TO_CF[method]
    cm = format_cell_methods(dim, cf_method, interval=interval)
    for name in measured_variables(out):
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
        measured = [name for name in measured_variables(ds) if dim in ds[name].dims]
        try:
            out = roll_and_agg(
                ds.drop_vars([n for n in ds.data_vars if n not in measured_variables(ds)]),
                window,
                dim,
                method,
                align=align,
                stride=stride_val,
            )
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

            # Per cell: the share of the window's rows with data at that cell.
            present = xr.Dataset({name: ds[name].notnull().astype("float64") for name in measured})
            if window > 1 or stride is not None:
                present = roll_and_agg(present, window, dim, "mean", align=align, stride=stride_val)
            best = []
            for name in measured:
                cov = present[name]
                out[f"{name}{COVERAGE_SUFFIX}"] = cov.astype("float32").assign_attrs(
                    _coverage_attrs(name)
                )
                out[name].attrs = _link_coverage(out[name].attrs, name)
                best.append(cov.max([d for d in cov.dims if d != dim]).values)
            out = _assign_coverage(out, dim, np.min(best, axis=0) if best else np.ones(n))
        agg_period = _rolling_aggregation_period(ds, dim, window, interval)
        return _stamp_attrs(out, dim, agg_period, method, interval, data_interval=native_interval)

    spec = _resolve_period(period)
    if native_interval and _durations_equal(spec["agg"], native_interval):
        import numpy as np
        import xarray as xr

        # Already at the period: each cell covers its own interval where it has data.
        out = ds.copy()
        n = out.sizes.get(dim, 0)
        if n:
            step_share = xr.DataArray(coverage_values(ds, dim), dims=[dim])
            best = []
            for name in [v for v in measured_variables(ds) if dim in ds[v].dims]:
                cov = coverage_variable(ds, name)
                if cov is None:
                    cov = (ds[name].notnull() * step_share).astype("float32")
                    out[f"{name}{COVERAGE_SUFFIX}"] = cov.assign_attrs(_coverage_attrs(name))
                    out[name].attrs = _link_coverage(ds[name].attrs, name)
                best.append(cov.max([d for d in cov.dims if d != dim]).values)
            out = _assign_coverage(out, dim, np.min(best, axis=0) if best else np.ones(n))
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
        out = _reduce_bins(ds, dim, spec, method, _bins(ds, dim, spec))
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
        out = _reduce_bins(ds, dim, spec, method, _bins(ds, dim, spec, end_time, start_time))
    else:
        out = _reduce_bins(ds, dim, spec, method, _bins(ds, dim, spec))

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

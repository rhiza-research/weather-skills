# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
#   "numpy",
#   "pandas",
#   "xarray",
# ]
# ///
"""Compute a daily climatology from a multi-year daily Zarr.

For each calendar day, takes the values on that month and day from every year and
writes their mean, sample standard deviation, optional quantiles and count. Calendar
days are the 366 days of a leap year, so Feb 29 rests on leap years only.

--window pools that many calendar days centred on each day, wrapping from Dec 31 to
Jan 1. --pool-members counts each ensemble member as one more value instead of
giving each member its own climatology. --min-fraction leaves a cell NaN when too
few of its possible values exist.

The result is repeated onto every date from --start-time to --end-time, in the same
layout as clim-fetch.
"""

import sys
import warnings

import numpy as np
import pandas as pd
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_dataset import detect_time_dim
from weather_skills_core.units import (
    AGGREGATION_PERIOD_ATTR,
    dequantify_dataset,
    parse_aggregation_period,
    stamp_data_interval,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


def _slot_1904(times) -> np.ndarray:
    """0-based slot of each date's month and day in the 1904 leap-year cycle."""
    times = pd.DatetimeIndex(times)
    same_day = pd.to_datetime({"year": 1904, "month": times.month, "day": times.day})
    return same_day.dt.day_of_year.to_numpy() - 1


@weather_skill(name="climatology", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--window",
    type=int,
    default=1,
    help="Odd number of calendar days pooled around each day (default: 1, no pooling).",
)
@weather_skill.argument(
    "--quantiles",
    help="Comma-separated quantiles between 0 and 1 (e.g. 0.333,0.667), written as V_qNN.",
)
@weather_skill.argument(
    "--pool-members",
    action="store_true",
    help="Treat ensemble members (number) as extra samples: one climatology for all members.",
)
@weather_skill.argument(
    "--min-fraction",
    type=float,
    default=0.0,
    help="Leave a cell NaN when fewer than this fraction of its possible values exist (default: 0).",
)
def climatology(
    ds, start_time, end_time, variable, window, quantiles, pool_members, min_fraction, **kwargs
):
    """Compute a daily climatology from a multi-year daily Zarr, laid onto a date range."""
    if window < 1 or window % 2 == 0:
        raise UsageError(f"--window must be a positive odd number of days; got {window}")
    if not 0 <= min_fraction <= 1:
        raise UsageError(f"--min-fraction must be between 0 and 1; got {min_fraction}")
    qs = [float(q) for q in quantiles.split(",")] if quantiles else []
    if any(not 0 < q < 1 for q in qs):
        raise UsageError(f"--quantiles must be between 0 and 1 (exclusive); got {quantiles}")
    time_dim = detect_time_dim(ds)
    times = pd.DatetimeIndex(ds[time_dim].values)
    # Dates may be sparse (weekly forecast inits), never closer than a day.
    spacing = np.diff(times.values) / np.timedelta64(1, "D")
    if spacing.size and spacing.min() < 1:
        raise UsageError(
            f"input has dates {spacing.min() * 24:g} hours apart; it must hold one value per day. "
            "Roll sub-daily data up with aggregate-temporal first."
        )
    if spacing.size and spacing.min() > 1:
        print(
            f"climatology: dates are at least {spacing.min():g} days apart, so most calendar days "
            "rest on few values; consider --window.",
            file=sys.stderr,
        )
    years = sorted(set(times.year))
    if len(years) < 2:
        raise UsageError(f"input spans only {years}; a climatology needs at least two years")

    names = variable or [n for n in ds.data_vars if time_dim in ds[n].dims]
    for name in names:
        if name not in ds.data_vars or time_dim not in ds[name].dims:
            raise UsageError(f"--variable {name!r} is not a data variable along {time_dim!r}")
        if pool_members and "number" not in ds[name].dims:
            raise UsageError(f"--pool-members: {name!r} has no ensemble member dim 'number'")
        # On a forecast archive the period describes the lead window in step
        # (e.g. weekly totals), not the spacing of the init dates in time.
        period = ds[name].attrs.get(AGGREGATION_PERIOD_ATTR)
        if (
            "step" not in ds[name].dims
            and period is not None
            and parse_aggregation_period(period) != parse_aggregation_period("1 day")
        ):
            raise UsageError(
                f"{name!r} holds {period} values; a daily climatology needs daily values. "
                "Roll it up or down to 1 day with aggregate-temporal first."
            )

    slots = _slot_1904(times)
    target = pd.date_range(start_time, end_time, freq="D")
    target_slots = _slot_1904(target)
    half = window // 2

    ds = dequantify_dataset(ds[names])
    out = xr.Dataset()
    for name in names:
        # Samples per date: the members when pooled, otherwise one.
        if pool_members:
            da = ds[name].transpose(time_dim, "number", ...)
        else:
            da = ds[name].transpose(time_dim, ...).expand_dims("_sample", axis=1)
        units = da.attrs.get("units")
        values = np.asarray(da.values)
        rest_shape = values.shape[2:]
        values = values.reshape(len(times), values.shape[1], -1)
        n_cells = values.shape[2]

        # Per-slot sums of values, squares, finite counts and possible counts over every year.
        total = np.zeros((366, n_cells))
        squares = np.zeros((366, n_cells))
        count = np.zeros((366, n_cells))
        possible = np.zeros((366, 1))
        for k in range(366):
            rows = values[slots == k].reshape(-1, n_cells).astype(np.float64)
            total[k] = np.nansum(rows, axis=0)
            squares[k] = np.nansum(rows**2, axis=0)
            count[k] = np.isfinite(rows).sum(axis=0)
            possible[k] = len(rows)

        # Pool the --window slots around each day, wrapping across the year end.
        total, squares, count, possible = (
            sum(np.roll(a, shift, axis=0) for shift in range(-half, half + 1))
            for a in (total, squares, count, possible)
        )
        enough = (count > 0) & (count >= min_fraction * possible)

        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(enough, total / count, np.nan)
            variance = (squares - count * mean**2) / (count - 1)
            std = np.where(enough & (count > 1), np.sqrt(np.clip(variance, 0, None)), np.nan)

        # Quantiles need the pooled values themselves, so only the slots in the output.
        quant = np.full((366, len(qs), n_cells), np.nan)
        for k in np.unique(target_slots) if qs else []:
            in_window = np.isin(slots, (k + np.arange(-half, half + 1)) % 366)
            rows = values[in_window].reshape(-1, n_cells).astype(np.float64)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN cells stay NaN
                quant[k] = np.nanquantile(rows, qs, axis=0)
        quant = np.where(enough[:, None, :], quant, np.nan)

        template = da.isel({time_dim: 0, da.dims[1]: 0}, drop=True)
        dims = (time_dim, *template.dims)
        coords = {**template.coords, time_dim: target}
        shape = (len(target), *rest_shape)
        attrs = {k: v for k, v in da.attrs.items() if k not in ("units", "cell_methods")}
        stats = {"avg": (mean, {}), "std": (std, {})}
        for i, q in enumerate(qs):
            stats[f"q{q * 100:02g}".replace(".", "p")] = (quant[:, i], {"quantile": q})
        for suffix, (stat, extra) in stats.items():
            out[f"{name}_{suffix}"] = xr.DataArray(
                stat[target_slots].reshape(shape),
                dims=dims,
                coords=coords,
                attrs={**attrs, "units": units, **extra},
            )
        # A coordinate, like aggregate-temporal's coverage: as a data variable
        # its name ("precip_count") would read as a precip field downstream.
        out = out.assign_coords(
            {
                f"{name}_count": xr.DataArray(
                    count[target_slots].reshape(shape).astype(np.int32),
                    dims=dims,
                    coords=coords,
                    attrs={"long_name": f"Number of values averaged for {name}", "units": "1"},
                )
            }
        )

        # Dates with no value anywhere stay NaN, never filled: say so.
        empty = out[f"{name}_count"].sum([d for d in dims if d != time_dim]) == 0
        if empty.any():
            dates = target[empty.values].strftime("%Y-%m-%d")
            print(
                f"climatology: WARNING: {name}: {len(dates)} of {len(target)} dates have no "
                f"value anywhere (left NaN, not filled): {', '.join(dates)}",
                file=sys.stderr,
            )

    out.attrs.update(
        Conventions="CF-1.13",
        climatology_start_time=times[0].strftime("%Y-%m-%d"),
        climatology_end_time=times[-1].strftime("%Y-%m-%d"),
        climatology_window_days=window,
        climatology_pooled_members=int(pool_members),
        climatology_min_fraction=min_fraction,
    )
    stamp_cf_attrs(out)
    return stamp_data_interval(out, period="1 day")


if __name__ == "__main__":
    climatology()

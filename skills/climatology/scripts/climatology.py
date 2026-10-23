# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/weather-skills/weather-skills-core@per-cell-coverage",
#   "numpy",
#   "pandas",
#   "xarray",
# ]
# ///
"""Compute a daily climatology (mean, standard deviation, sample count per calendar day) from a multi-year daily Zarr.

Each date maps to its month and day in the 1904 leap-year cycle (366 slots, Feb 29
only from leap years). One pass over the 366 slots sums values, squares and counts
over every year; an optional --window pools neighbouring slots (circular across the
year end) before the mean and the sample standard deviation are taken. The result is
laid onto --start-time/--end-time like clim-fetch, so it is a drop-in replacement.
"""

import sys

import numpy as np
import pandas as pd
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import measured_variables, stamp_cf_attrs
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
def climatology(ds, start_time, end_time, variable, window, **kwargs):
    """Compute a daily climatology from a multi-year daily Zarr, laid onto a date range."""
    if window < 1 or window % 2 == 0:
        raise UsageError(f"--window must be a positive odd number of days; got {window}")
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

    names = variable or [n for n in measured_variables(ds) if time_dim in ds[n].dims]
    for name in names:
        if name not in ds.data_vars or time_dim not in ds[name].dims:
            raise UsageError(f"--variable {name!r} is not a data variable along {time_dim!r}")
        period = ds[name].attrs.get(AGGREGATION_PERIOD_ATTR)
        if period is not None and parse_aggregation_period(period) != parse_aggregation_period(
            "1 day"
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
        da = ds[name].transpose(time_dim, ...)
        units = da.attrs.get("units")
        values = np.asarray(da.values)
        rest_shape = values.shape[1:]
        values = values.reshape(len(times), -1)

        # Per-slot sums of values, squares and finite counts over every year.
        total = np.zeros((366, values.shape[1]))
        squares = np.zeros((366, values.shape[1]))
        count = np.zeros((366, values.shape[1]))
        for k in range(366):
            rows = values[slots == k].astype(np.float64)
            total[k] = np.nansum(rows, axis=0)
            squares[k] = np.nansum(rows**2, axis=0)
            count[k] = np.isfinite(rows).sum(axis=0)

        # Pool the --window slots around each day, wrapping across the year end.
        total = sum(np.roll(total, shift, axis=0) for shift in range(-half, half + 1))
        squares = sum(np.roll(squares, shift, axis=0) for shift in range(-half, half + 1))
        count = sum(np.roll(count, shift, axis=0) for shift in range(-half, half + 1))

        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(count > 0, total / count, np.nan)
            variance = (squares - count * mean**2) / (count - 1)
            std = np.where(count > 1, np.sqrt(np.clip(variance, 0, None)), np.nan)

        template = da.isel({time_dim: 0}, drop=True)
        dims = (time_dim, *template.dims)
        coords = {**template.coords, time_dim: target}
        shape = (len(target), *rest_shape)
        attrs = {k: v for k, v in da.attrs.items() if k not in ("units", "cell_methods")}
        out[f"{name}_avg"] = xr.DataArray(
            mean[target_slots].reshape(shape),
            dims=dims,
            coords=coords,
            attrs={**attrs, "units": units},
        )
        out[f"{name}_std"] = xr.DataArray(
            std[target_slots].reshape(shape),
            dims=dims,
            coords=coords,
            attrs={**attrs, "units": units},
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
    )
    stamp_cf_attrs(out)
    return stamp_data_interval(out, period="1 day")


if __name__ == "__main__":
    climatology()

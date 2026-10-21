"""Member-based predictors: the chance each model's members put the onset trigger in each lead week.

Usage: python members_predictors.py ARCHIVE WORK CYCLE [cumulus]

For ECMWF (WeatherBench2 reforecast, model cycle CYCLE: 2019 -> 1999-2018, 2022 -> 2002-2021),
Thursday Sep-Dec issuances, and optionally Cumulus paired to those inits (latest Cumulus init on or
before, lag 0-1 day):
  indicator --rule icpac-onset --first-in 7d --season-start 09-15 --probability --time-dim step
gives, at lead day d, the share of members whose first ICPAC onset on or after 15 Sep (by valid
date) falls in days d..d+6. The predictor for lead week k is that value at d = 7k (plus the
Cumulus lag). The onset rule needs 27 days of look-ahead, so only weeks 1-2 are decidable.
Grids end on the 1.5 deg cells in WORK/grid.zarr (coarsen for Cumulus).

Writes WORK/ecmwf_hind.zarr and WORK/cumulus_hind.zarr: probability(time=init, step=0/7 d,
latitude, longitude). Own code: reshaping WeatherBench2's issuance x hindcast-year layout, and
reading the two leads; everything else is skills.
"""

import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

ARCHIVE, WORK, CYCLE = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
CUMULUS = len(sys.argv) > 4 and sys.argv[4] == "cumulus"
WEEKS = np.array([0, 7])
BOX = "8.25/27.75/-6.75/48.75"
tmp = WORK / "tmp"
tmp.mkdir(parents=True, exist_ok=True)


def by_day(src, out):
    """Share of members whose first onset since 15 Sep falls in the 7 days from each lead day."""
    run("indicator", "indicator", "-i", src, "--rule", "icpac-onset", "--first-in", "7d",
        "--season-start", "09-15", "--probability", "--time-dim", "step", "-v", "precip", "-o", out)
    return xr.open_zarr(out)["probability"].load()


def weekly(p_by, lag_days):
    """The first-in-7d probability at the start of each lead week. lag shifts an earlier init."""
    days = (p_by.step.values / np.timedelta64(1, "D")).astype(int)
    rows = [p_by.isel(step=int(np.flatnonzero(days == lag_days + w)[0])).drop_vars("step", errors="ignore")
            for w in WEEKS]
    da = xr.concat(rows, dim="step").assign_coords(step=WEEKS.astype("timedelta64[D]").astype("timedelta64[ns]"))
    da.attrs = {"units": "1", "aggregation_period": "7 day",
                "long_name": "Share of members whose first onset since 15 Sep falls in the week"}
    return da


# ── ECMWF: WeatherBench2 issuance x hindcast-year -> one init axis (own code: no skill reads it) ──
wb = xr.open_zarr(ARCHIVE / "ecmwf_reforecast.zarr")
ft = wb.forecast_time.to_index()
thursdays = ft[(ft.year == CYCLE) & (ft.dayofweek == 3)]
sub = wb.sel(forecast_time=thursdays)
tp = (sub["total_precipitation_24hr"] * 1000).astype("float32")  # metres -> mm per day
inits = pd.DatetimeIndex(sub["time"].values.ravel())
cube = tp.stack(init=("forecast_time", "hindcast_year")).transpose("init", "number", "prediction_timedelta", ...)
cube = cube.drop_vars(["init", "forecast_time", "hindcast_year", "time"], errors="ignore")
cube = cube.assign_coords(init=inits.values).rename(init="time", prediction_timedelta="step")
# WeatherBench2 stores the lead as whole days (integers); make it a timedelta step.
lead = cube.step.values
lead_days = lead / np.timedelta64(1, "D") if np.issubdtype(lead.dtype, np.timedelta64) else lead
cube = cube.assign_coords(step=lead_days.astype(int).astype("timedelta64[D]").astype("timedelta64[ns]"))
ecm = cube.sortby("time").to_dataset(name="precip")
ecm["precip"].attrs = {"units": "mm day-1", "data_interval": "1 day"}
src = tmp / "ecmwf_members.zarr"
shutil.rmtree(src, ignore_errors=True)
ecm.to_zarr(src, mode="w")
clipped = tmp / "ecmwf_members_box.zarr"
shutil.rmtree(clipped, ignore_errors=True)
run("clip-region", "clip", "-i", src, "--bbox", BOX, "-o", clipped)
p = by_day(clipped, tmp / "ecmwf_by_day.zarr")
ew = weekly(p, 0).to_dataset(name="probability")
shutil.rmtree(WORK / "ecmwf_hind.zarr", ignore_errors=True)
ew.to_zarr(WORK / "ecmwf_hind.zarr", mode="w")
print(f"ecmwf_hind.zarr: {ew.sizes['time']} inits ({inits.year.min()}-{inits.year.max()}), cycle {CYCLE}")

if CUMULUS:
    cu = xr.open_zarr(ARCHIVE / "cumulus_hindcast.zarr")
    cu_inits = pd.DatetimeIndex(cu.time.values)
    pairs = []
    for d in pd.DatetimeIndex(ew.time.values):
        earlier = cu_inits[cu_inits <= d]
        if len(earlier) and (d - earlier[-1]).days <= 1:
            pairs.append((d, earlier[-1]))
    wanted = sorted({c for _, c in pairs})
    sel = tmp / "cumulus_sel.zarr"
    shutil.rmtree(sel, ignore_errors=True)
    run("select", "select_dim", "-i", ARCHIVE / "cumulus_hindcast.zarr", "--dim", "time",
        *[a for c in wanted for a in ("--value", c.strftime("%Y-%m-%d"))], "-o", sel)
    pc = by_day(sel, tmp / "cumulus_by_day.zarr")
    rows = [weekly(pc.sel(time=c), (d - c).days).expand_dims(time=[d]) for d, c in pairs]
    cw = xr.concat(rows, dim="time").to_dataset(name="probability")
    fine = tmp / "cumulus_weekly_1deg.zarr"
    shutil.rmtree(fine, ignore_errors=True)
    cw.to_zarr(fine, mode="w")
    shutil.rmtree(WORK / "cumulus_hind.zarr", ignore_errors=True)
    run("coarsen", "coarsen", "-i", fine, "--reference-grid", WORK / "grid.zarr", "--method", "conservative",
        "-o", WORK / "cumulus_hind.zarr")
    years = pd.Series([d.year for d, _ in pairs]).value_counts().sort_index().to_dict()
    print(f"cumulus_hind.zarr: {len(pairs)} inits paired; per season {years}")

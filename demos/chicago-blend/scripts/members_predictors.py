"""Member-based predictors: the chance each model's members put the onset trigger in each lead week.

Usage: python members_predictors.py ARCHIVE WORK CYCLE [cumulus] [gefs]

For ECMWF (WeatherBench2 reforecast, model cycle CYCLE: 2019 -> 1999-2018, 2022 -> 2002-2021),
Thursday Sep-Dec issuances, and optionally Cumulus and GEFS paired to those inits (the latest init on
or before, lag 0-1 day: Cumulus every 2 days; the GEFS reforecast on Wednesdays to 2019, then the
operational GEFS, daily, from ARCHIVE/gefs_operational.zarr when it is there):
  indicator --rule icpac-onset --first-in Wd --season-start 09-15 --probability --time-dim step
for W = 7 and 14 gives, at each lead day, the share of members whose first ICPAC onset on or after
15 Sep (by valid date) falls in the next W days. The predictor is that share at the init's valid
date (lead 0, or the Cumulus lag): the chance the rains have started within 7 / 14 days of the init.
The onset rule needs 27 days of look-ahead, so 14 days is as far as a 46-day forecast decides; GEFS
runs 35 days, so its 14-day share is NaN (undecided) and GEFS enters the 7-day window only.
Grids end on the 1.5 deg cells in WORK/grid.zarr (coarsen for Cumulus and the 1 deg GEFS reforecast).

Writes WORK/ecmwf_hind.zarr, WORK/cumulus_hind.zarr and WORK/gefs_hind.zarr: probability(time=init,
step=0, window=7/14, latitude, longitude). Own code: reshaping WeatherBench2's issuance x
hindcast-year layout and the GEFS reforecast's IRIDL layout, and reading the shares at the init;
everything else is skills.
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
CUMULUS, GEFS = "cumulus" in sys.argv[4:], "gefs" in sys.argv[4:]
WINDOWS = [7, 14]  # days from the init
BOX = "8.25/27.75/-6.75/48.75"
tmp = WORK / "tmp"
tmp.mkdir(parents=True, exist_ok=True)


def by_day(src, stem, var="precip"):
    """For each window W: the share of members whose first onset since 15 Sep falls in the W days
    from each lead day (indicator --first-in Wd --probability along step)."""
    out = {}
    for w in WINDOWS:
        path = tmp / f"{stem}_{w}d.zarr"
        shutil.rmtree(path, ignore_errors=True)
        run("indicator", "indicator", "-i", src, "--rule", "icpac-onset", "--first-in", f"{w}d",
            "--season-start", "09-15", "--probability", "--time-dim", "step", "-v", var, "-o", path)
        out[w] = xr.open_zarr(path)["probability"].load()
    return out


def windowed(p_by, lag_days):
    """Each window's share read at the init's valid date (lead day lag), stacked along window,
    with a one-entry step (0) so mme-fit looks up the obs at the init date."""
    rows = []
    for w in WINDOWS:
        p = p_by[w]
        days = (p.step.values / np.timedelta64(1, "D")).astype(int)
        rows.append(p.isel(step=int(np.flatnonzero(days == lag_days)[0])).drop_vars("step", errors="ignore"))
    da = xr.concat(rows, dim=pd.Index(WINDOWS, name="window")).expand_dims(step=[np.timedelta64(0, "ns")])
    da.attrs = {"units": "1", "long_name": "Share of members whose first onset since 15 Sep falls within the window"}
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
p = by_day(clipped, "ecmwf_by_day")
ew = windowed(p, 0).to_dataset(name="probability")
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
    pc = by_day(sel, "cumulus_by_day")
    rows = [windowed({w: pc[w].sel(time=c) for w in WINDOWS}, (d - c).days).expand_dims(time=[d]) for d, c in pairs]
    cw = xr.concat(rows, dim="time").to_dataset(name="probability")
    fine = tmp / "cumulus_windows_1deg.zarr"
    shutil.rmtree(fine, ignore_errors=True)
    cw.to_zarr(fine, mode="w")
    shutil.rmtree(WORK / "cumulus_hind.zarr", ignore_errors=True)
    run("coarsen", "coarsen", "-i", fine, "--reference-grid", WORK / "grid.zarr", "--method", "conservative",
        "-o", WORK / "cumulus_hind.zarr")
    years = pd.Series([d.year for d, _ in pairs]).value_counts().sort_index().to_dict()
    print(f"cumulus_hind.zarr: {len(pairs)} inits paired; per season {years}")

if GEFS:
    ec_inits = pd.DatetimeIndex(ew.time.values)
    parts = []
    # Reforecast (Wednesdays 1999-2019, 11 members, 1 deg). Own code: its IRIDL layout (init_time in
    # days since 1960, member, integer-day prediction_timedelta, lat/lon) onto the standard names.
    rf = xr.open_zarr(ARCHIVE / "gefs_reforecast.zarr", decode_times=False)
    rf_inits = pd.Timestamp("1960-01-01") + pd.to_timedelta(rf.init_time.values.astype("float64"), unit="D")
    pairs = []
    for d in ec_inits:
        earlier = rf_inits[rf_inits <= d]
        if len(earlier) and (d - earlier[-1]).days <= 1:
            pairs.append((d, earlier[-1]))
    wanted = sorted({c for _, c in pairs})
    idx = [int(np.flatnonzero(rf_inits == c)[0]) for c in wanted]
    std = rf.isel(init_time=idx).rename(init_time="time", member="number", prediction_timedelta="step",
                                         lat="latitude", lon="longitude")
    std = std.assign_coords(time=pd.DatetimeIndex(wanted).values, number=std.number.values.astype("int16"),
                            step=std.step.values.astype("timedelta64[D]").astype("timedelta64[ns]"))
    std = std.transpose("time", "number", "step", "latitude", "longitude")
    std["precip"].attrs = {"units": "mm day-1", "data_interval": "1 day"}
    src = tmp / "gefs_reforecast_sel.zarr"
    shutil.rmtree(src, ignore_errors=True)
    std.to_zarr(src, mode="w")
    pg = by_day(src, "gefs_rf_by_day")
    rows = [windowed({w: pg[w].sel(time=c) for w in WINDOWS}, (d - c).days).expand_dims(time=[d]) for d, c in pairs]
    fine = tmp / "gefs_rf_windows_1deg.zarr"
    shutil.rmtree(fine, ignore_errors=True)
    xr.concat(rows, dim="time").to_dataset(name="probability").to_zarr(fine, mode="w")
    coarse = tmp / "gefs_rf_windows.zarr"
    shutil.rmtree(coarse, ignore_errors=True)
    run("coarsen", "coarsen", "-i", fine, "--reference-grid", WORK / "grid.zarr", "--method", "conservative",
        "-o", coarse)
    parts.append(xr.open_zarr(coarse).load())
    years = [d.year for d, _ in pairs]
    # Operational (daily inits from Oct 2020, 31 members, already on the 1.5 deg cells): the same day.
    ops_path = ARCHIVE / "gefs_operational.zarr"
    if ops_path.exists():
        ops_inits = pd.DatetimeIndex(xr.open_zarr(ops_path).time.values)
        same = [d for d in ec_inits if d in ops_inits]
        sel = tmp / "gefs_ops_sel.zarr"
        shutil.rmtree(sel, ignore_errors=True)
        run("select", "select_dim", "-i", ops_path, "--dim", "time",
            *[a for d in same for a in ("--value", d.strftime("%Y-%m-%d"))], "-o", sel)
        clipped = tmp / "gefs_ops_box.zarr"
        shutil.rmtree(clipped, ignore_errors=True)
        run("clip-region", "clip", "-i", sel, "--bbox", BOX, "-o", clipped)
        var = next(iter(xr.open_zarr(clipped).data_vars))
        po = by_day(clipped, "gefs_ops_by_day", var)
        rows = [windowed({w: po[w].sel(time=d) for w in WINDOWS}, 0).expand_dims(time=[d]) for d in same]
        parts.append(xr.concat(rows, dim="time").to_dataset(name="probability"))
        years += [d.year for d in same]
    gw = xr.concat(parts, dim="time").sortby("time")
    # 35-day GEFS decides early onsets only, so its 14-day shares exist just where the rains came
    # early: a biased sample. Keep GEFS to the 7-day window.
    gw = gw.where(gw["window"] == 7)
    shutil.rmtree(WORK / "gefs_hind.zarr", ignore_errors=True)
    gw.to_zarr(WORK / "gefs_hind.zarr", mode="w")
    print(f"gefs_hind.zarr: {gw.sizes['time']} inits paired; per season {pd.Series(years).value_counts().sort_index().to_dict()}")

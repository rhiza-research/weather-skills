"""Climatological probability of the event in each window from the init, as a forecast-shaped predictor.

From the per-season observed event series (members_events.py: first onset within 7 / 14 days, NaN
once the onset has happened), for each init date and window: the share of reference seasons with
the first onset within the window from the same day of the season, among seasons where it is still
possible.
Training samples use every 1981-2025 season except their own; 2026 uses the
analog seasons (analog) or all seasons (all).
"""

import os
import sys

import numpy as np
import pandas as pd
import xarray as xr

D = sys.argv[1]
ANALOGS = [1982, 1994, 1997, 2006, 2015, 2019, 2023]
YEARS = range(1981, 2026)

series = {}
for y in YEARS:
    ev = xr.open_zarr(f"{D}/chirps/ev_{y}.zarr")["indicator"].load()
    dos = (pd.DatetimeIndex(ev.time.values) - pd.Timestamp(f"{y}-10-01")).days
    series[y] = ev.assign_coords(time=dos).rename(time="dos")
E = xr.concat([series[y] for y in YEARS], dim=pd.Index(list(YEARS), name="year"), join="inner")
E = E.transpose("year", "dos", "window", "latitude", "longitude")
hind = xr.open_zarr(f"{D}/ecmwf_hind.zarr")
# Same cell order as the forecasts (both on the 1.5° grid; CHIRPS may be stored north-first).
E = E.sel(
    latitude=hind.latitude.values, longitude=hind.longitude.values, method="nearest", tolerance=0.01
)


def clim_for(inits, years_for):
    rows = []
    for init in pd.DatetimeIndex(inits):
        start = (init - pd.Timestamp(f"{init.year}-10-01")).days
        ref = E.sel(year=years_for(init.year))
        # Days before the observed season (15 Sep) have no event series: NaN, so the sample drops.
        rows.append(
            ref.reindex(dos=[start]).isel(dos=0).mean("year", skipna=True).values
        )
    return rows


def write(rows, inits, name, like):
    da = xr.DataArray(
        np.stack(rows)[:, None].astype("float32"),
        dims=("time", "step", "window", "latitude", "longitude"),
        coords={
            "time": pd.DatetimeIndex(inits).values,
            "step": [np.timedelta64(0, "ns")],
            "window": E.window.values,
            "latitude": like.latitude.values,
            "longitude": like.longitude.values,
        },
    )
    ds = da.to_dataset(name="probability")
    ds["probability"].attrs.update(
        units="1",
        long_name="Climatological probability of the first onset within the window",
    )
    ds.to_zarr(f"{D}/{name}.zarr", mode="w", consolidated=True)
    return da


loo = write(
    clim_for(hind.time.values, lambda y: [x for x in YEARS if x != y]),
    hind.time.values,
    "clim_hind",
    hind,
)
init26 = [pd.Timestamp(os.environ.get("EC_INIT", "2026-10-15"))]
an = write(clim_for(init26, lambda y: ANALOGS), init26, "clim_2026_analog", hind)
al = write(clim_for(init26, lambda y: list(YEARS)), init26, "clim_2026_all", hind)
print("training clim mean by window", loo.mean(["time", "step", "latitude", "longitude"]).values.round(3))
print("2026 analog clim by window", an.mean(["time", "step", "latitude", "longitude"]).values.round(3))
print("2026 all-years clim by window", al.mean(["time", "step", "latitude", "longitude"]).values.round(3))

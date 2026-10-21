"""Cumulus OND hindcast predictors on the ECMWF inits.

Usage: python prep_cumulus.py WORK      (needs ARCHIVE/$CUMULUS_STORE and WORK/ecmwf_hind.zarr)

For each ECMWF hindcast init it takes the latest Cumulus init on or before it
(Cumulus runs every 2 days, so the lag is 0 or 1 day) and averages the ensemble
mean (all members) over the same valid-date weeks: Cumulus step d covers day
[init + d, init + d + 1), so ECMWF lead week k is Cumulus steps lag + 7k .. lag + 7k + 6.
Writes WORK/cumulus_hind_1deg.zarr (time = ECMWF init, step 0/7/14/21 days, 1 deg),
mm day-1 and aggregation_period 7 day; regrid it onto the ECMWF grid with coarsen.
"""

import os
import sys

import numpy as np
import pandas as pd
import xarray as xr

WORK = sys.argv[1]
STEPS = np.array([0, 7, 14, 21])

ec = xr.open_zarr(f"{WORK}/ecmwf_hind.zarr")
# CUMULUS_STORE picks the hindcast store in ARCHIVE (default cumulus_reforecast.zarr, run
# heartbreaking-portfolio). For weights applied to the live forecast use
# cumulus_gratis-czajkowski.zarr, the run that matches it.
store = os.environ.get("CUMULUS_STORE", "cumulus_reforecast.zarr")
cu = xr.open_zarr(f"{os.environ['ARCHIVE']}/{store}")["precip"]
# A margin around the 1.5 deg Kenya cells so conservative regridding covers them.
cu = cu.sel(latitude=slice(6, -6), longitude=slice(31, 44))
cu_inits = pd.DatetimeIndex(cu.time.values)
lead = (cu.step.values / np.timedelta64(1, "D")).astype(int)

pairs = []
for init in pd.DatetimeIndex(ec.time.values):
    earlier = cu_inits[cu_inits <= init]
    if len(earlier) and (init - earlier[-1]).days <= 1:
        pairs.append((init, earlier[-1]))
sel = cu.sel(time=[p[1] for p in pairs]).mean("number").load()
rows = []
for i, (init, cinit) in enumerate(pairs):
    lag = (init - cinit).days
    one = sel.isel(time=i)
    rows.append(
        np.stack(
            [
                one.isel(step=(lead >= lag + s) & (lead < lag + s + 7)).mean("step").values
                for s in STEPS
            ]
        )
    )
da = xr.DataArray(
    np.stack(rows).astype("float32"),
    dims=("time", "step", "latitude", "longitude"),
    coords={
        "time": [p[0] for p in pairs],
        "step": STEPS.astype("timedelta64[D]").astype("timedelta64[ns]"),
        "latitude": sel.latitude.values,
        "longitude": sel.longitude.values,
    },
)
ds = da.to_dataset(name="precip")
ds["precip"].attrs.update(
    units="mm day-1", aggregation_period="7 day", long_name="Cumulus weekly precipitation"
)
ds["step"].attrs.update(standard_name="forecast_period")
ds["time"].attrs.update(standard_name="forecast_reference_time")
ds.to_zarr(f"{WORK}/cumulus_hind_1deg.zarr", mode="w", consolidated=True)
years = pd.Series([p[0].year for p in pairs]).value_counts().sort_index().to_dict()
lags = pd.Series([(a - b).days for a, b in pairs]).value_counts().sort_index().to_dict()
print(
    f"cumulus paired with {len(pairs)} of {ec.sizes['time']} ECMWF inits; per season {years}; lag days {lags}"
)

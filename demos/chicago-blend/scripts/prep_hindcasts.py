"""Write ECMWF and GEFS OND hindcasts for Kenya as weather-skills forecast Zarrs.

ECMWF: WeatherBench2 extended-range reforecast, daily ensemble mean, Thursday
issuances in Oct-Nov of the cycle year ECMWF_CYCLE (default 2019: 3 Oct - 28 Nov,
hindcast years 1999-2018; 2022 gives 2002-2021).
GEFS: Sheerwater GEFS reforecast (1.0 deg, 11 members, Wednesday inits); for each
ECMWF init the latest GEFS init at least one day earlier, aligned on valid date.
Both: weekly mean rate (mm day-1) for lead weeks starting 0, 7, 14, 21 days after
the ECMWF init, on dims (time=init, step, latitude, longitude).
"""

import os
import sys

import numpy as np
import pandas as pd
import xarray as xr

OUT = sys.argv[1]
# Local copies from 0_download_archives.py when ARCHIVE is set; else the cloud stores.
ARCHIVE = os.environ.get("ARCHIVE")
STEPS = np.array([0, 7, 14, 21])


def stamp(da, name):
    ds = da.astype("float32").to_dataset(name=name)
    ds[name].attrs.update(
        units="mm day-1", aggregation_period="7 day", long_name="Precipitation rate"
    )
    ds["step"].attrs.update(standard_name="forecast_period")
    ds["time"].attrs.update(standard_name="forecast_reference_time")
    return ds


# ── ECMWF ──
if ARCHIVE:
    wb = xr.open_zarr(f"{ARCHIVE}/ecmwf_reforecast.zarr")
else:
    wb = xr.open_zarr(
        "gs://weatherbench2/datasets/ifs_extended_range/daily/"
        "ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr",
        storage_options={"token": "anon"},
    )
ft = wb.forecast_time.to_index()
# Model cycle: the issuance year whose 20 hindcast years are used (2019 -> 1999-2018, 2022 -> 2002-2021).
CYCLE = int(os.environ.get("ECMWF_CYCLE", "2019"))
issues = ft[(ft.year == CYCLE) & ft.month.isin([10, 11]) & (ft.dayofweek == 3)]
tp = wb["total_precipitation_24hr"].sel(
    forecast_time=issues, latitude=slice(-4.5, 4.5), longitude=slice(33, 42)
)
tp = (tp * 1000).load()  # m per day -> mm per day; lead d covers day [init+d, init+d+1)
inits = pd.DatetimeIndex(wb["time"].sel(forecast_time=issues).values.ravel())
lead = tp["prediction_timedelta"].values.astype("timedelta64[D]").astype(int)
weekly = np.stack(
    [
        tp.isel(prediction_timedelta=(lead >= s) & (lead < s + 7))
        .mean("prediction_timedelta")
        .values
        for s in STEPS
    ],
    axis=2,
)  # (forecast_time, hindcast_year, step, lat, lon)
weekly = weekly.reshape(-1, len(STEPS), *weekly.shape[-2:])
ec = xr.DataArray(
    weekly,
    dims=("time", "step", "latitude", "longitude"),
    coords={
        "time": inits.values,
        "step": STEPS.astype("timedelta64[D]").astype("timedelta64[ns]"),
        "latitude": tp.latitude.values,
        "longitude": tp.longitude.values,
    },
).sortby("time")
ec = ec.sel(time=ec.time.dt.year >= 1999)
stamp(ec, "precip").to_zarr(f"{OUT}/ecmwf_hind.zarr", mode="w", consolidated=True)
print("ecmwf", dict(ec.sizes), str(ec.time.values[0])[:10], str(ec.time.values[-1])[:10])

# ── GEFS, lagged onto the ECMWF inits ──
if ARCHIVE:
    g = xr.open_zarr(f"{ARCHIVE}/gefs_reforecast.zarr", decode_times=False)["precip"]
else:
    g = xr.open_zarr(
        "gs://sheerwater-datalake/caches/gefs_chc_raw/global1_0_precip.zarr",
        zarr_format=3,
        decode_times=False,
        consolidated=False,
    )["precip"]
g_inits = pd.Timestamp("1960-01-01") + pd.to_timedelta(g.init_time.values, unit="D")
g = g.assign_coords(init_time=g_inits).sel(lat=slice(-6, 6), lon=slice(31, 44))
pairs = []
for init in pd.DatetimeIndex(ec.time.values):
    earlier = g_inits[g_inits <= init - pd.Timedelta(days=1)]
    if len(earlier) and (init - earlier[-1]).days <= 7:
        pairs.append((init, earlier[-1]))
gsel = g.sel(init_time=[p[1] for p in pairs]).mean("member").load()
glead = gsel["prediction_timedelta"].values.astype(int)
rows = []
for i, (init, ginit) in enumerate(pairs):
    lag = (init - ginit).days
    one = gsel.isel(init_time=i)
    rows.append(
        np.stack(
            [
                one.isel(prediction_timedelta=(glead >= lag + s) & (glead < lag + s + 7))
                .mean("prediction_timedelta")
                .values
                for s in STEPS
            ]
        )
    )
ge = xr.DataArray(
    np.stack(rows),
    dims=("time", "step", "latitude", "longitude"),
    coords={
        "time": [p[0] for p in pairs],
        "step": STEPS.astype("timedelta64[D]").astype("timedelta64[ns]"),
        "latitude": gsel.lat.values,
        "longitude": gsel.lon.values,
    },
)
stamp(ge, "precip").to_zarr(f"{OUT}/gefs_hind_1deg.zarr", mode="w", consolidated=True)
lags = pd.Series([(a - b).days for a, b in pairs]).value_counts().sort_index().to_dict()
print(
    "gefs",
    dict(ge.sizes),
    "lag days:",
    lags,
    "unpaired ECMWF inits:",
    ec.sizes["time"] - len(pairs),
)

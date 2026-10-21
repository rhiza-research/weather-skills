"""Hindcasts with ensemble members for Ethiopia + Kenya + Uganda, written straight to the bucket.

Meant to run next to the data (a Coiled or GCE VM in us-central1):
    JOB=ecmwf_reforecast | ecmwf_realtime | gefs_operational  python cloud_members.py

  ecmwf_reforecast   WeatherBench2 IFS extended-range reforecast, 10 perturbed members, every
                     Sep-Dec issuance 2016-2022 x 20 hindcast years (1996-2021), 46 daily leads.
                     total_precipitation_24hr in metres per day, as in the source.
  ecmwf_realtime     WeatherBench2 IFS extended-range real-time forecasts, 50 members, every
                     Sep-Dec init 2016-2022, 46 daily leads. Metres per day, as in the source.
  gefs_operational   dynamical.org GEFS 35-day, 31 members, Sep-Dec inits 2020-2025. The 3- and
                     6-hourly rates become daily means (weighted by interval length) in mm day-1,
                     step = start of each day, 0..34.

Writes OUT/<job>.zarr (default OUT=gs://sheerwater-datalake/gates-demo/archive). Each block is
saved as OUT/<job>.parts/NNNNN.zarr and a rerun skips finished blocks; the joined store is
written once every block exists, then the parts are removed. LIMIT=N stops after N blocks (a test).
"""

import os
import time

import gcsfs
import numpy as np
import pandas as pd
import xarray as xr

JOB = os.environ["JOB"]
OUT = os.environ.get("OUT", "gs://sheerwater-datalake/gates-demo/archive").rstrip("/")
LIMIT = int(os.environ.get("LIMIT", "0"))
MONTHS = [9, 10, 11, 12]
# Ethiopia + Kenya + Uganda: 14.85N-4.70S, 29.57-47.96E (resolve-region ETH, KEN, UGA).
N, W, S, E = 14.846, 29.573, -4.702, 47.960
fs = gcsfs.GCSFileSystem()
WB2 = "gs://weatherbench2/datasets/ifs_extended_range/daily"

if JOB == "ecmwf_reforecast":
    src = xr.open_zarr(f"{WB2}/ifs-ext-reforecast-full-single-level-avg.zarr", storage_options={"token": "anon"})
    src = src[["total_precipitation_24hr"]]
    dim, step_size = "forecast_time", 4
elif JOB == "ecmwf_realtime":
    src = xr.open_zarr(f"{WB2}/ifs-ext-full-single-level-avg.zarr", storage_options={"token": "anon"})
    src = src[["total_precipitation_24hr"]]
    dim, step_size = "time", 4
elif JOB == "gefs_operational":
    import dynamical_catalog

    src = dynamical_catalog.open("noaa-gefs-forecast-35-day")[["precipitation_surface"]]
    dim, step_size = "init_time", 4
else:
    raise SystemExit(f"unknown JOB {JOB!r}")

times = pd.DatetimeIndex(src[dim].values)
keep = times.month.isin(MONTHS)
if JOB == "gefs_operational":
    keep &= times.year <= 2025
src = src.isel({dim: np.flatnonzero(keep)})
if JOB.startswith("ecmwf"):
    # 1.5 deg cells covering the box; WeatherBench2 longitudes run 0-358.5.
    src = src.sel(latitude=slice(-6.0, 15.0), longitude=slice(28.5, 48.0))
else:
    # 0.25 deg points covering every 1.5 deg cell fully (edges at -6.75..15.75, 27.75..48.75);
    # dynamical latitude is north-first.
    src = src.sel(latitude=slice(15.75, -6.75), longitude=slice(27.75, 48.75))


def gefs_daily(block):
    """3/6-hourly rates (labelled at interval end) -> daily mean rate in mm day-1."""
    lead = block.lead_time.values
    width = np.diff(np.concatenate([[np.timedelta64(0, "ns")], lead])).astype("timedelta64[s]").astype(float)
    day = ((lead - np.timedelta64(1, "s")) // np.timedelta64(1, "D")).astype(int)
    rate = block["precipitation_surface"] * 86400  # kg m-2 s-1 -> mm day-1
    weighted = (rate * xr.DataArray(width, dims="lead_time")).groupby(xr.DataArray(day, dims="lead_time", name="day"))
    totals = weighted.sum("lead_time", skipna=False)
    widths = xr.DataArray(width, dims="lead_time").groupby(xr.DataArray(day, dims="lead_time", name="day")).sum()
    daily = (totals / widths).sel(day=slice(0, 34)).rename(day="step")
    daily = daily.assign_coords(step=(np.arange(daily.sizes["step"]) * np.timedelta64(1, "D")).astype("timedelta64[ns]"))
    # Onto the ECMWF 1.5 deg cells (centres at multiples of 1.5, lat -6..15, lon 28.5..48): the
    # area mean of the 0.25 deg points in each cell, points on a cell edge counted half, weighted
    # by cos(latitude). 36x fewer cells, which keeps the store small.
    lat, lon = daily.latitude.values, daily.longitude.values
    centre_lat, centre_lon = np.arange(-6.0, 15.1, 1.5), np.arange(28.5, 48.1, 1.5)

    def inside(points, centres):
        d = np.abs(points[None, :] - centres[:, None])
        return np.where(d < 0.75 - 1e-6, 1.0, np.where(d < 0.75 + 1e-6, 0.5, 0.0))

    wy = xr.DataArray(inside(lat, centre_lat) * np.cos(np.deg2rad(lat))[None, :],
                      dims=("cell_lat", "latitude"), coords={"cell_lat": centre_lat})
    wx = xr.DataArray(inside(lon, centre_lon), dims=("cell_lon", "longitude"), coords={"cell_lon": centre_lon})
    # xr.dot sums as it multiplies; a plain product would first build every point x cell pair.
    total = xr.dot(daily, wy, wx, dim=["latitude", "longitude"])
    coarse = (total / (wy.sum("latitude") * wx.sum("longitude"))).rename(cell_lat="latitude", cell_lon="longitude")
    ds = coarse.astype("float32").to_dataset(name="precip")
    ds["precip"].attrs = {"units": "mm day-1", "long_name": "GEFS daily mean precipitation rate, 1.5 deg area mean"}
    return ds


final = f"{OUT}/{JOB}.zarr"
if fs.exists(final + "/zarr.json") or fs.exists(final + "/.zmetadata"):
    print(f"{JOB}: already in the bucket, skipped")
    raise SystemExit(0)
parts = f"{OUT}/{JOB}.parts"
n = src.sizes[dim]
names, t0 = [], time.time()
for k, i in enumerate(range(0, n, step_size)):
    part = f"{parts}/{i:05d}.zarr"
    names.append(part)
    if fs.exists(part + "/.done"):
        continue
    block = src.isel({dim: slice(i, i + step_size)}).load()
    if JOB == "gefs_operational":
        block = gefs_daily(block)
    for v in block.variables.values():
        v.encoding = {}
    block.to_zarr(fs.get_mapper(part), mode="w")
    fs.pipe(part + "/.done", b"")
    print(f"{JOB}: {min(i + step_size, n)}/{n} {dim} ({(time.time() - t0) / 60:.1f} min)", flush=True)
    if LIMIT and k + 1 >= LIMIT:
        print(f"{JOB}: stopped after {LIMIT} blocks (LIMIT)")
        raise SystemExit(0)

joined = xr.concat([xr.open_zarr(fs.get_mapper(p)) for p in names], dim=dim)
for v in joined.variables.values():
    v.encoding = {}
joined.to_zarr(fs.get_mapper(final), mode="w")
fs.rm(parts, recursive=True)
print(f"{JOB}: done in {(time.time() - t0) / 60:.1f} min; {dict(joined.sizes)}")

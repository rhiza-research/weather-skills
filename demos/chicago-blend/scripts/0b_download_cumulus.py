"""One-time download of the Cumulus (Aurora S2S) precipitation hindcast for a region.

Usage: python 0b_download_cumulus.py ARCHIVE_DIR [N/W/S/E]

Settings (environment):
  CUMULUS_RUN     hindcast run under ml_reforecasts/ (default heartbreaking-portfolio)
  CUMULUS_VAR     precipitation field in that run (default total_precipitation_24h_acc)
  CUMULUS_MONTHS  init months to keep, e.g. 10,11 (default all)
  CUMULUS_YEARS   init years to keep, e.g. 2016-2021 (default all)
The default run writes cumulus_reforecast.zarr; any other run writes
cumulus_<run>.zarr. The live forecast (cumulus-fetch, run utmost-plane) matches
gratis-czajkowski, not heartbreaking-portfolio: use CUMULUS_RUN=gratis-czajkowski
CUMULUS_VAR=total_precipitation_24h_acc_imerg for weights applied to live forecasts.

Source: Azure Blob italynorthdata/data/ml_reforecasts/heartbreaking-portfolio/
{cf,pf}/total_precipitation_24h_acc/data/YYYY-MM-DD-00-HHHH.nc - one global 1 deg
file per init and lead. cf holds the control member, pf members 1-9. Inits run
every 2 days, 2016-01-03..2022-12-31 (1301); leads 24..1104 h. Each value is the
rainfall of the 24 hours ending at that lead, so lead 24 h covers day
[init, init + 1). The files carry no units attr; values are mm (about 1-2 mm a
day on average over East Africa, up to about 200), so they are stored as mm day-1.

Needs AZURE_STORAGE_SAS_TOKEN (a read SAS for the italynorthdata/data container,
the one the cumulus-fetch skill uses). Never print it.

Writes ARCHIVE_DIR/cumulus_reforecast.zarr: precip(time=init, number=0..9 with 0
the control, step=0..45 days as interval start, latitude, longitude) over the box
plus a 1 deg margin; about 1.8 h for the full hindcast. Each month of inits is
saved under cumulus_reforecast.parts/ (temporary name, then renamed), so a rerun
resumes; the joined store appears only once complete, and then reruns skip it.
"""

import io
import math
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import adlfs
import numpy as np
import pandas as pd
import xarray as xr

ARCHIVE = Path(sys.argv[1])
ARCHIVE.mkdir(parents=True, exist_ok=True)
n, w, s, e = (
    float(x)
    for x in (sys.argv[2] if len(sys.argv) > 2 else "17.998/21.888/-26.742/51.134").split("/")
)
RUN = os.environ.get("CUMULUS_RUN", "heartbreaking-portfolio")
VAR = os.environ.get("CUMULUS_VAR", "total_precipitation_24h_acc")
out = ARCHIVE / (
    "cumulus_reforecast.zarr" if RUN == "heartbreaking-portfolio" else f"cumulus_{RUN}.zarr"
)
if out.exists():
    print(f"{out.name}: already downloaded, skipped")
    sys.exit(0)

token = os.environ["AZURE_STORAGE_SAS_TOKEN"]
token = token.split("?", 1)[1] if "?" in token else token
fs = adlfs.AzureBlobFileSystem(account_name="italynorthdata", sas_token=token)
BASE = f"data/ml_reforecasts/{RUN}"
LEADS = list(range(24, 1105, 24))
# 1 deg cells (centres on whole degrees) covering the box with a 1 deg margin.
lat_n, lat_s = math.ceil(n) + 1, math.floor(s) - 1
lon_w, lon_e = math.floor(w) - 1, math.ceil(e) + 1

names = fs.ls(f"{BASE}/cf/{VAR}/data")
inits = pd.DatetimeIndex(sorted({pd.Timestamp(p.split("/")[-1][:10]) for p in names}))
if os.environ.get("CUMULUS_MONTHS"):
    inits = inits[inits.month.isin([int(m) for m in os.environ["CUMULUS_MONTHS"].split(",")])]
if os.environ.get("CUMULUS_YEARS"):
    first, last = (int(y) for y in os.environ["CUMULUS_YEARS"].split("-"))
    inits = inits[(inits.year >= first) & (inits.year <= last)]
print(
    f"{len(inits)} inits {inits[0].date()}..{inits[-1].date()}; lat {lat_s}..{lat_n}, lon {lon_w}..{lon_e}"
)


def read(path):
    ds = xr.open_dataset(io.BytesIO(fs.cat_file(path)), engine="h5netcdf")
    da = ds[VAR].sel(latitude=slice(lat_n, lat_s), longitude=slice(lon_w % 360, lon_e % 360))
    return da.load()


def one_init(init, pool):
    stamp = init.strftime("%Y-%m-%d")
    cf = [f"{BASE}/cf/{VAR}/data/{stamp}-00-{h:04d}.nc" for h in LEADS]
    pf = [f"{BASE}/pf/{VAR}/data/{stamp}-00-{h:04d}.nc" for h in LEADS]
    got = list(pool.map(read, cf + pf))
    control = xr.concat(got[:46], dim="step").expand_dims(number=[0])
    members = xr.concat(got[46:], dim="step")
    da = xr.concat([control, members], dim="number")
    da = da.assign_coords(step=np.arange(46).astype("timedelta64[D]").astype("timedelta64[ns]"))
    return da.expand_dims(time=[init.to_datetime64()])


t0 = time.time()
parts = out.with_suffix(".parts")
parts.mkdir(exist_ok=True)
months = inits.to_period("M").unique()
names = []
with ThreadPoolExecutor(16) as pool:
    for k, month in enumerate(months):
        part = parts / f"{month}.zarr"
        names.append(part)
        if part.exists():
            continue
        block = xr.concat(
            [one_init(i, pool) for i in inits[inits.to_period("M") == month]], dim="time"
        )
        ds = (
            block.astype("float32")
            .transpose("time", "number", "step", "latitude", "longitude")
            .to_dataset(name="precip")
        )
        ds["precip"].attrs.update(
            units="mm day-1",
            long_name="Cumulus (Aurora S2S) 24 h precipitation, per day",
            cumulus_run=RUN,
            cumulus_variable=VAR,
        )
        tmp = part.with_suffix(".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        ds.to_zarr(tmp, mode="w")
        tmp.rename(part)
        print(
            f"{out.name}: {month} ({k + 1}/{len(months)} months, {(time.time() - t0) / 60:.0f} min)",
            flush=True,
        )

joined = xr.concat([xr.open_zarr(p) for p in names], dim="time")
for v in joined.variables.values():
    v.encoding = {}
tmp = out.with_suffix(".tmp")
shutil.rmtree(tmp, ignore_errors=True)
joined.to_zarr(tmp, mode="w")
tmp.rename(out)
shutil.rmtree(parts)
print(f"{out.name}: done in {(time.time() - t0) / 60:.1f} min; {dict(joined.sizes)}")

"""One-time download of daily CHIRPS for the East Africa box at its native 0.05 deg.

Usage: python 1c_chirps_eastafrica.py ARCHIVE_DIR [N/W/S/E]

Source: dynamical.org ucsb-chc-chirps-analysis-final (CHIRPS final, 1981 to about a
month ago), chunked one year x 3 deg, so a year of the box reads in seconds.
Writes ARCHIVE_DIR/chirps_eastafrica.zarr: precip(time, latitude, longitude) in
mm day-1, latitude north-first as in the source. The box defaults to East Africa
(resolve-region "East Africa"). Years are appended to chirps_eastafrica.tmp one at a
time; a rerun resumes after the last complete year, and the store is renamed to its
final name only when complete, so an existing one is skipped.
"""

import shutil
import sys
import time
from pathlib import Path

import dynamical_catalog
import pandas as pd
import xarray as xr

ARCHIVE = Path(sys.argv[1])
n, w, s, e = (
    float(x)
    for x in (sys.argv[2] if len(sys.argv) > 2 else "17.998/21.888/-26.742/51.134").split("/")
)
out = ARCHIVE / "chirps_eastafrica.zarr"
if out.exists():
    print(f"{out.name}: already downloaded, skipped")
    sys.exit(0)

src = dynamical_catalog.open("ucsb-chc-chirps-analysis-final")[["precipitation_surface"]]
src = src.sel(latitude=slice(n, s), longitude=slice(w, e)).rename(precipitation_surface="precip")
last_day = pd.Timestamp(src.time.values[-1])
tmp = out.with_suffix(".tmp")
first_year = 1981
if tmp.exists():
    done = pd.Timestamp(xr.open_zarr(tmp).time.values[-1])
    if done.month == 12 and done.day == 31:
        first_year = done.year + 1
    else:  # an append stopped part-way through a year: start again
        shutil.rmtree(tmp)
t0 = time.time()
for year in range(first_year, last_day.year + 1):
    block = src.sel(time=slice(f"{year}-01-01", f"{year}-12-31")).load()
    for v in block.variables.values():
        v.encoding = {}
    # The source stores kg m-2 s-1 (mm per second); convert to mm day-1.
    block["precip"] = (block["precip"] * 86400).astype("float32")
    block["precip"].attrs = {"units": "mm day-1", "long_name": "CHIRPS final daily precipitation"}
    if tmp.exists():
        block.to_zarr(tmp, append_dim="time")
    else:
        # Zarr chunks of about a year x 15 deg; set through encoding, so no dask is needed.
        block["precip"].encoding = {"chunks": (365, 300, 300)}
        block.to_zarr(tmp, mode="w")
    print(f"{out.name}: {year} ({(time.time() - t0) / 60:.1f} min)", flush=True)
tmp.rename(out)
ds = xr.open_zarr(out)
print(
    f"{out.name}: done; {dict(ds.sizes)}, {str(ds.time.values[0])[:10]}..{str(ds.time.values[-1])[:10]}"
)

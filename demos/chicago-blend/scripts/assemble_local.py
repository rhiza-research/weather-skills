"""Cut already-downloaded data to Ethiopia + Kenya + Uganda for the demo archive (no downloads).

Usage: python assemble_local.py SOURCE_ARCHIVE FINAL_ARCHIVE

  chirps_daily.zarr        CHIRPS final daily 1981-2026 at 0.05 deg, converted from the
                           source's kg m-2 s-1 to mm day-1. The box reaches 0.75 deg past
                           the 1.5 deg cell centres so it can be regridded conservatively.
  gefs_reforecast.zarr     GEFS reforecast 1999-2019, 11 members, 1 deg (same layout as before).
  tahmo/tahmo_YYYY.zarr    TAHMO daily station data for Ethiopia, Kenya and Uganda stations,
                           with tahmo/stations.csv (station_id, name, latitude, longitude, country).
Each output is written as <name>.tmp and renamed when complete; existing outputs are skipped.
"""

import csv
import shutil
import sys
from pathlib import Path

import xarray as xr

SRC, FINAL = Path(sys.argv[1]), Path(sys.argv[2])
FINAL.mkdir(parents=True, exist_ok=True)
# 1.5 deg cells covering Ethiopia + Kenya + Uganda: centres lat -6..15, lon 28.5..48.
LAT_S, LAT_N, LON_W, LON_E = -6.75, 15.75, 27.75, 48.75


def write(ds, out, chunks=None):
    for v in ds.variables.values():
        v.encoding = {}
    if chunks:
        for name, c in chunks.items():
            ds[name].encoding = {"chunks": c}
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    ds.to_zarr(tmp, mode="w")
    tmp.rename(out)


out = FINAL / "chirps_daily.zarr"
if not out.exists():
    c = xr.open_zarr(SRC / "chirps_eastafrica.zarr")
    c = c.sel(latitude=slice(LAT_N, LAT_S), longitude=slice(LON_W, LON_E))
    # September-January days only: the season every demo analysis uses (1.5 GB, not 3.8).
    c = c.sel(time=c.time.dt.month.isin([9, 10, 11, 12, 1])).load()
    # The source store holds kg m-2 s-1 (mm per second) although it was labelled mm day-1.
    c["precip"] = (c["precip"] * 86400).astype("float32")
    c["precip"].attrs = {"units": "mm day-1", "long_name": "CHIRPS final daily precipitation"}
    write(c, out, {"precip": (153, 150, 150)})
print("chirps_daily.zarr", dict(xr.open_zarr(out).sizes))

out = FINAL / "gefs_reforecast.zarr"
if not out.exists():
    g = xr.open_zarr(SRC / "gefs_reforecast.zarr", decode_times=False)
    g = g.sel(lat=slice(LAT_S - 0.5, LAT_N + 0.5), lon=slice(LON_W - 0.5, LON_E + 0.5)).load()
    write(g, out)
print("gefs_reforecast.zarr", dict(xr.open_zarr(out, decode_times=False).sizes))

stations = [ln.split("\t") for ln in (SRC / "tahmo" / "stations.tsv").read_text().splitlines()]
keep = sorted({s[0] for s in stations if s[4] in ("ET", "KE", "UG")})
(FINAL / "tahmo").mkdir(exist_ok=True)
for path in sorted((SRC / "tahmo").glob("tahmo_*.zarr")):
    out = FINAL / "tahmo" / path.name
    if not out.exists():
        t = xr.open_zarr(path)
        t = t.sel(station_id=[s for s in t.station_id.values if s in keep]).load()
        write(t, out)
kept = [ln for ln in stations if ln[0] in keep]
# A CSV with a header (names can hold commas, so they are quoted where needed).
with open(FINAL / "tahmo" / "stations.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["station_id", "name", "latitude", "longitude", "country"])
    writer.writerows(kept)
print("tahmo:", len(keep), "stations in ET/KE/UG,", len(list((FINAL / "tahmo").glob("tahmo_*.zarr"))), "years")

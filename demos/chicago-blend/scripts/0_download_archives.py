"""One-time download of the full ECMWF and GEFS hindcast archives for a region.

Usage: python 0_download_archives.py ARCHIVE_DIR [N/W/S/E]

The box defaults to East Africa (resolve-region "East Africa":
17.998/21.888/-26.742/51.134). It is widened to whole 1.5 deg ECMWF cells, and
GEFS gets a further 1.5 deg margin so it can be regridded conservatively onto
those cells. Any Kenya analysis is then a subset of the archive.

Writes, keeping each source's own variables, units, dims and coords (only the
region is cut), so prep_hindcasts.py reads them exactly like the cloud stores:
  ecmwf_reforecast.zarr - WeatherBench2 IFS extended-range reforecast,
      daily ensemble mean: every issuance 2016-01-04..2022-12-29 (Mon/Thu) x 20
      hindcast years x 46 daily leads at 1.5 deg. Precip in metres per day.
  gefs_reforecast.zarr - Sheerwater GEFS reforecast (needs Google ADC with
      read access to gs://sheerwater-datalake): every Wednesday init
      1998-12-30..2019-08-28 x 11 members x 35 daily leads at 1.0 deg. Precip
      in mm/day; init_time in days since 1960-01-01. Same source (CHC SubC
      EMC-GEFSv12_CPC, cached by sheerwater gefs_chc_raw).
  gefs_reforecast_tmp2m.zarr - the same GEFS reforecast for 2 m temperature (deg C):
      1071 Wednesday inits 1999-2019 x 11 members x 35 daily leads.
  gefs_operational.zarr - operational GEFS ensemble mean (sheerwater gfs_raw, from
      dynamical.org): daily inits 2020-10-01..2026-05-20 (2058) x 36 daily leads
      (0..840 h), 1.5 deg. Together with the reforecast this gives GEFS rainfall
      for 1999-2026. The precip values are mm/day although the stored units
      attribute still says kg m-2 s-1 (a stale attr from the source).
A finished store is skipped without touching the network; delete it to download
again. An interrupted download resumes from its last finished block.
"""

import math
import shutil
import sys
import time
from pathlib import Path

import xarray as xr

ARCHIVE = Path(sys.argv[1])
ARCHIVE.mkdir(parents=True, exist_ok=True)
n, w, s, e = (
    float(x)
    for x in (sys.argv[2] if len(sys.argv) > 2 else "17.998/21.888/-26.742/51.134").split("/")
)
# Whole 1.5 deg cells (centres at multiples of 1.5) covering the box.
lat_s, lat_n = 1.5 * math.floor(s / 1.5), 1.5 * math.ceil(n / 1.5)
lon_w, lon_e = 1.5 * math.floor(w / 1.5), 1.5 * math.ceil(e / 1.5)
print(f"ECMWF cells lat {lat_s}..{lat_n}, lon {lon_w}..{lon_e}; GEFS 1.5 deg wider")


def download(src, dim, step, out, decode_times):
    """Copy SRC to OUT in blocks along DIM, resuming after an interruption.

    DECODE_TIMES must match how SRC was opened: each block stores its dates with
    its own reference ("days since <first date>"), so blocks of decoded dates are
    joined decoded, never as raw numbers.

    Each finished block is saved under OUT.parts/ (written to a temporary name,
    then renamed), so a rerun skips it. When every block is there they are joined
    into OUT, again via a temporary name, so OUT exists only when complete.
    """
    if out.exists():
        print(f"{out.name}: already downloaded, skipped")
        return
    t0 = time.time()
    parts = out.with_suffix(".parts")
    parts.mkdir(exist_ok=True)
    n = src.sizes[dim]
    names = []
    for i in range(0, n, step):
        part = parts / f"{i:05d}.zarr"
        names.append(part)
        if part.exists():
            continue
        block = src.isel({dim: slice(i, i + step)}).load()
        for v in block.variables.values():
            v.encoding = {}
        tmp = part.with_suffix(".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        block.to_zarr(tmp, mode="w")
        tmp.rename(part)
        print(f"{out.name}: {min(i + step, n)}/{n} {dim}", flush=True)
    joined = xr.concat([xr.open_zarr(p, decode_times=decode_times) for p in names], dim=dim)
    for v in joined.variables.values():
        v.encoding = {}
    tmp = out.with_suffix(".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    joined.to_zarr(tmp, mode="w")
    tmp.rename(out)
    shutil.rmtree(parts)
    print(f"{out.name}: done in {(time.time() - t0) / 60:.1f} min")


ecmwf_out = ARCHIVE / "ecmwf_reforecast.zarr"
if not ecmwf_out.exists():
    wb = xr.open_zarr(
        "gs://weatherbench2/datasets/ifs_extended_range/daily/"
        "ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr",
        storage_options={"token": "anon"},
    )
    ec = wb[["total_precipitation_24hr"]].sel(
        latitude=slice(lat_s, lat_n), longitude=slice(lon_w % 360, lon_e % 360)
    )
    download(ec, "forecast_time", 50, ecmwf_out, decode_times=True)
else:
    print(f"{ecmwf_out.name}: already downloaded, skipped")

gefs_out = ARCHIVE / "gefs_reforecast.zarr"
if not gefs_out.exists():
    g = xr.open_zarr(
        "gs://sheerwater-datalake/caches/gefs_chc_raw/global1_0_precip.zarr",
        zarr_format=3,
        decode_times=False,
        consolidated=False,
    )
    g = g[["precip"]].sel(lat=slice(lat_s - 1.5, lat_n + 1.5), lon=slice(lon_w - 1.5, lon_e + 1.5))
    # One member at a time keeps memory to a few hundred MB.
    download(g, "member", 1, gefs_out, decode_times=False)
else:
    print(f"{gefs_out.name}: already downloaded, skipped")

tmp2m_out = ARCHIVE / "gefs_reforecast_tmp2m.zarr"
if not tmp2m_out.exists():
    t = xr.open_zarr(
        "gs://sheerwater-datalake/caches/gefs_chc_raw/global1_0_tmp2m.zarr",
        zarr_format=3,
        decode_times=False,
        consolidated=False,
    )
    t = t[["tmp2m"]].sel(lat=slice(lat_s - 1.5, lat_n + 1.5), lon=slice(lon_w - 1.5, lon_e + 1.5))
    download(t, "member", 1, tmp2m_out, decode_times=False)
else:
    print(f"{tmp2m_out.name}: already downloaded, skipped")

ops_out = ARCHIVE / "gefs_operational.zarr"
if not ops_out.exists():
    o = xr.open_zarr("gs://sheerwater-datalake/caches/gfs_raw/global1_5_deterministic_precip.zarr")
    o = o[["precip"]].sel(lat=slice(lat_s, lat_n), lon=slice(lon_w, lon_e))
    o["precip"].attrs.update(
        units="mm day-1", units_note="source attr says kg m-2 s-1; values are mm/day"
    )
    # Blocks of 50 inits line up with the source's 10-init chunks.
    download(o, "init_time", 50, ops_out, decode_times=True)
else:
    print(f"{ops_out.name}: already downloaded, skipped")

for path in (ecmwf_out, gefs_out, tmp2m_out, ops_out):
    ds = xr.open_zarr(path, decode_times=False)
    print(path.name, dict(ds.sizes))

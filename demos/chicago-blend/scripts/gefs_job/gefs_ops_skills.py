"""GEFS operational hindcasts for Ethiopia + Kenya + Uganda, built only with weather skills.

Runs on a cloud VM next to the data. For every Sep-Dec init 2020-2025 it runs
  dynamical-fetch    noaa-gefs-forecast-35-day, all 31 members, 0.25 deg, clipped to the box
  aggregate-temporal --period daily --time-dim step     (3/6-hourly rates -> daily means per member)
  coarsen            --method conservative onto the ECMWF 1.5 deg cells
and saves the result as OUT/gefs_operational.parts/YYYY-MM-DD.zarr (a .done marker once complete,
so a rerun skips it). When every init is there, concat --dim time joins them into
OUT/gefs_operational.zarr and the parts are removed.

Settings: OUT (default gs://sheerwater-datalake/gates-demo/archive), WORKERS (default 8 inits at
once), LIMIT (stop after this many inits: a test).
"""

import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dynamical_catalog
import gcsfs
import numpy as np
import pandas as pd
import xarray as xr

HERE = Path(__file__).resolve().parent
SKILLS = HERE / "skills"
OUT = os.environ.get("OUT", "gs://sheerwater-datalake/gates-demo/archive").rstrip("/")
WORKERS = int(os.environ.get("WORKERS", "8"))
LIMIT = int(os.environ.get("LIMIT", "0"))
# Edges of the 1.5 deg cells covering Ethiopia + Kenya + Uganda (centres lat -6..15, lon 28.5..48).
BBOX = "15.75/27.75/-6.75/48.75"
WORK = Path("/tmp/gefs_ops")
WORK.mkdir(exist_ok=True)
fs = gcsfs.GCSFileSystem()
parts = f"{OUT}/gefs_operational.parts"

# The 1.5 deg target grid for coarsen --reference-grid.
grid = WORK / "grid_1p5.zarr"
if not grid.exists():
    lat, lon = np.arange(-6.0, 15.1, 1.5), np.arange(28.5, 48.1, 1.5)
    g = xr.Dataset({"precip": (("latitude", "longitude"), np.zeros((lat.size, lon.size), "float32"))},
                   coords={"latitude": lat, "longitude": lon})
    g["precip"].attrs["units"] = "mm day-1"
    g.to_zarr(grid, mode="w", consolidated=True)

inits = pd.DatetimeIndex(dynamical_catalog.open("noaa-gefs-forecast-35-day").init_time.values)
inits = inits[inits.month.isin([9, 10, 11, 12]) & (inits.year <= 2025)]
todo = [d.strftime("%Y-%m-%d") for d in inits if not fs.exists(f"{parts}/{d:%Y-%m-%d}.zarr/.done")]
if LIMIT:
    todo = todo[:LIMIT]
print(f"{len(inits)} inits Sep-Dec 2020-2025; {len(todo)} to do", flush=True)


def skill(name, script, *args):
    subprocess.run([sys.executable, str(SKILLS / name / "scripts" / script), *map(str, args)],
                   check=True, capture_output=True, text=True)


def one(day):
    t0 = time.time()
    raw, daily, coarse = WORK / f"raw_{day}.zarr", WORK / f"daily_{day}.zarr", WORK / f"{day}.zarr"
    try:
        skill("dynamical-fetch", "fetch.py", "--dataset", "noaa-gefs-forecast-35-day", "--date", day,
              "--bbox", BBOX, "-v", "precip", "-o", raw)
        skill("aggregate-temporal", "aggregate.py", "-i", raw, "--period", "daily", "--time-dim", "step", "-o", daily)
        skill("coarsen", "coarsen.py", "-i", daily, "--reference-grid", grid, "--method", "conservative", "-o", coarse)
        fs.put(str(coarse), f"{parts}/{day}.zarr", recursive=True)
        fs.pipe(f"{parts}/{day}.zarr/.done", b"")
        return day, time.time() - t0, None
    except subprocess.CalledProcessError as exc:
        return day, time.time() - t0, (exc.stderr or "")[-400:]
    finally:
        for p in (raw, daily, coarse):
            shutil.rmtree(p, ignore_errors=True)


t_start = time.time()
failed = []
with ThreadPoolExecutor(WORKERS) as pool:
    for k, (day, secs, err) in enumerate(pool.map(one, todo), 1):
        if err:
            failed.append(day)
            print(f"{day}: FAILED after {secs:.0f}s: {err}", flush=True)
        else:
            print(f"{day}: {secs:.0f}s ({k}/{len(todo)}, {(time.time() - t_start) / 60:.1f} min)", flush=True)

done = sorted(p.rstrip("/").split("/")[-1] for p in fs.ls(parts) if fs.exists(f"{p}/.done")) if fs.exists(parts) else []
if LIMIT or failed or len(done) < len(inits):
    print(f"not joining: {len(done)} of {len(inits)} inits in the bucket, {len(failed)} failed this run")
    raise SystemExit(1 if failed else 0)

local = WORK / "parts"
local.mkdir(exist_ok=True)
for name in done:
    fs.get(f"{parts}/{name}", str(local / name), recursive=True)
inputs = [a for name in done for a in ("-i", local / name)]
skill("concat", "concat.py", *inputs, "--dim", "time", "-o", WORK / "gefs_operational.zarr")
fs.put(str(WORK / "gefs_operational.zarr"), f"{OUT}/gefs_operational.zarr", recursive=True)
fs.rm(parts, recursive=True)
print(f"gefs_operational.zarr: {len(done)} inits joined in {(time.time() - t_start) / 60:.1f} min")

"""Set the 2026 blends to 1 where CHIRPS already shows the rainy season started before the init.

Usage: python started.py ARCHIVE WORK INIT NAME [NAME ...]      e.g. ... 2026-10-15 p2026_cumulus clim_2026_all

  1. indicator --rule icpac-onset --cumulative on ARCHIVE/live/chirps_2026.zarr (CHIRPS final and
     preliminary from 15 Sep 2026, on the 1.5 deg cells): 1 from the first confirmed onset on.
  2. clip-region it to the analysis box (WORK/grid.zarr's cells).
  3. Each WORK/NAME.zarr (a 2026 blend or climatology: the chance the rains have started by a date)
     becomes 1 where the onset is confirmed by the day before INIT (own code: a where).
An onset needs 21 days of rain data after its trigger to be confirmed, so onsets that triggered in
the last three weeks before the CHIRPS end date are not known yet and stay unmasked.
Writes WORK/started.zarr (the confirmed-onset series) and rewrites the NAME files.
"""

import shutil
import sys
from pathlib import Path

import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

ARCHIVE, WORK, INIT, NAMES = Path(sys.argv[1]), Path(sys.argv[2]), pd.Timestamp(sys.argv[3]), sys.argv[4:]
BOX = "8.25/27.75/-6.75/48.75"
tmp = WORK / "tmp"
tmp.mkdir(parents=True, exist_ok=True)

onset = tmp / "chirps_2026_onset.zarr"
shutil.rmtree(onset, ignore_errors=True)
run("indicator", "indicator", "-i", ARCHIVE / "live" / "chirps_2026.zarr", "--rule", "icpac-onset",
    "--cumulative", "-o", onset)
shutil.rmtree(WORK / "started.zarr", ignore_errors=True)
run("clip-region", "clip", "-i", onset, "--bbox", BOX, "-o", WORK / "started.zarr")
started = xr.open_zarr(WORK / "started.zarr")["indicator"].sel(time=INIT - pd.Timedelta(days=1)).load() == 1
print(f"rains already started (confirmed) by {INIT - pd.Timedelta(days=1):%Y-%m-%d}: {int(started.sum())} cells")

for name in NAMES:
    path = WORK / f"{name}.zarr"
    ds = xr.open_zarr(path).load()
    mask = started.reindex_like(ds, method="nearest", tolerance=0.01).fillna(False).drop_vars("time", errors="ignore")
    ds["probability"] = ds["probability"].where(~mask, 1.0)
    ds.attrs["comment"] = "1 where CHIRPS shows the rainy season already started before the init."
    ds.to_zarr(path, mode="w")


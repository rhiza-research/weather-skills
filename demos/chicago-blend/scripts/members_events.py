"""Observed OND onset events from the archive CHIRPS, on the 1.5 deg cells south of 8 N (skills only).

Usage: python members_events.py ARCHIVE WORK

  1. clip-region a live ECMWF init to the analysis box -> WORK/grid.zarr (the 1.5 deg target grid:
     6 S - 7.5 N, 28.5 - 48 E, 10 x 14 cells; northern Ethiopia, whose main rains are June-September,
     is left out).
  2. clip-region ARCHIVE/chirps_daily_1p5.zarr (CHIRPS already coarsened conservatively onto the
     archive's 1.5 deg cells) to the same box (WORK/chirps15.zarr).
  3. For each season 1981-2025: select 15 Sep - 31 Jan, run indicator --rule icpac-onset
     --first-in 7d and --first-in 14d (1 if the season's first onset falls within 7 / 14 days of the
     date, 0 if not yet, NaN once past), and stack the two along a new window dim (7, 14). Own
     code: concat refuses inputs with different aggregation periods, which these are by design.
  4. concat every season into WORK/obs_event.zarr.
Seasons already in WORK/chirps are skipped.
"""

import sys
from pathlib import Path

import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

ARCHIVE, WORK = Path(sys.argv[1]), Path(sys.argv[2])
BOX = "8.25/27.75/-6.75/48.75"  # edges of the 1.5 deg cells centred 6 S - 7.5 N, 28.5 - 48 E
(WORK / "chirps").mkdir(parents=True, exist_ok=True)

grid = WORK / "grid.zarr"
if not grid.exists():
    live = sorted((ARCHIVE / "live" / "ecmwf").glob("ecmwf_*.zarr"))[-1]
    run("clip-region", "clip", "-i", live, "--bbox", BOX, "-o", grid)

coarse = WORK / "chirps15.zarr"
if not coarse.exists():
    run("clip-region", "clip", "-i", ARCHIVE / "chirps_daily_1p5.zarr", "--bbox", BOX, "-o", coarse)

for y in range(1981, 2026):
    ev = WORK / "chirps" / f"ev_{y}.zarr"
    if ev.exists():
        continue
    days = [d.strftime("%Y-%m-%d") for d in pd.date_range(f"{y}-09-15", f"{y + 1}-01-31")]
    season = WORK / "chirps" / f"c15_{y}.zarr"
    run("select", "select_dim", "-i", coarse, "--dim", "time",
        *[a for d in days for a in ("--value", d)], "-o", season)
    for w in (7, 14):
        run("indicator", "indicator", "-i", season, "--rule", "icpac-onset", "--first-in", f"{w}d",
            "-o", WORK / "chirps" / f"ev{w}_{y}.zarr")
    both = [xr.open_zarr(WORK / "chirps" / f"ev{w}_{y}.zarr")["indicator"] for w in (7, 14)]
    stacked = xr.concat(both, dim=pd.Index([7, 14], name="window"), combine_attrs="drop")
    stacked.attrs = {"units": "1", "long_name": "First ICPAC onset of the season within the window (days)"}
    stacked.to_dataset(name="indicator").to_zarr(ev, mode="w")

inputs = [a for y in range(1981, 2026) for a in ("-i", WORK / "chirps" / f"ev_{y}.zarr")]
run("concat", "concat", *inputs, "--dim", "time", "-o", WORK / "obs_event.zarr")
print("obs_event.zarr written")

"""Cut the Cumulus hindcast and the live forecasts for the demo archive, with weather skills only.

Usage: python assemble_live.py SOURCE_ARCHIVE FINAL_ARCHIVE

  cumulus_hindcast.zarr      clip-region of cumulus_gratis-czajkowski.zarr (Sep-Dec 2016-2024, 30 members)
  live/ecmwf/ecmwf_D.zarr     clip-region of each live ECMWF 46-day init (101 members)
  live/cumulus/cumulus_D.zarr clip-region of each live Cumulus init (30 members)
  live/gefs/gefs_D.zarr       coarsen --method conservative of each live GEFS init (31 members, daily)
                              onto the ECMWF 1.5 deg cells
The box is Ethiopia + Kenya + Uganda out to the edges of the 1.5 deg cells (15.75/27.75/-6.75/48.75).
Existing outputs are skipped; each is written by the skill itself, which writes atomically.
"""

import sys
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

SRC, FINAL = Path(sys.argv[1]), Path(sys.argv[2])
BBOX = "15.75/27.75/-6.75/48.75"

out = FINAL / "cumulus_hindcast.zarr"
if not out.exists():
    run("clip-region", "clip", "-i", SRC / "cumulus_gratis-czajkowski.zarr", "--bbox", BBOX, "-o", out)
print("cumulus_hindcast.zarr", dict(xr.open_zarr(out).sizes))

grid = FINAL / "live" / "grid_1p5.zarr"
if not grid.exists():
    lat, lon = np.arange(-6.0, 15.1, 1.5), np.arange(28.5, 48.1, 1.5)
    g = xr.Dataset({"precip": (("latitude", "longitude"), np.zeros((lat.size, lon.size), "float32"))},
                   coords={"latitude": lat, "longitude": lon})
    g["precip"].attrs["units"] = "mm day-1"
    g.to_zarr(grid, mode="w", consolidated=True)

for model in ("ecmwf", "cumulus", "gefs"):
    (FINAL / "live" / model).mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted((SRC / "live_inits" / model).glob(f"{model}_2026-*.zarr")):
        out = FINAL / "live" / model / f.name
        if out.exists():
            continue
        if model == "gefs":
            run("coarsen", "coarsen", "-i", f, "--reference-grid", grid, "--method", "conservative", "-o", out)
        else:
            run("clip-region", "clip", "-i", f, "--bbox", BBOX, "-o", out)
        n += 1
    print(f"live {model}: {n} new, {len(list((FINAL / 'live' / model).glob('*.zarr')))} in all")

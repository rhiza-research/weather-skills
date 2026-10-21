"""Live member predictors for one init, built the same way as the hindcast ones (members_predictors.py).

Usage: python members_live.py ARCHIVE WORK INIT [MODEL ...]      e.g. ... 2026-10-15 ecmwf cumulus

For each model and window W = 7, 14 days: indicator --rule icpac-onset --first-in Wd --season-start
09-15 --probability --time-dim step on ARCHIVE/live/MODEL/MODEL_INIT.zarr, read at lead 0 (the share
of members whose first onset since 15 Sep falls within W days of the init), put on WORK/grid.zarr (clip-region for ECMWF's and GEFS's 1.5 deg grids, coarsen for Cumulus's 1 deg).
Writes WORK/live/MODEL_wk.zarr: probability(step=0, window=7/14, latitude, longitude).
"""

import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

ARCHIVE, WORK, INIT = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
MODELS = sys.argv[4:] or ["ecmwf", "cumulus"]
WINDOWS = [7, 14]  # days from the init
BOX = "8.25/27.75/-6.75/48.75"
tmp = WORK / "tmp"
(WORK / "live").mkdir(parents=True, exist_ok=True)
tmp.mkdir(exist_ok=True)

for model in MODELS:
    src = ARCHIVE / "live" / model / f"{model}_{INIT}.zarr"
    var = list(xr.open_zarr(src).data_vars)[0]
    rows = []
    for w in WINDOWS:
        by = tmp / f"live_{model}_by_day_{w}d.zarr"
        shutil.rmtree(by, ignore_errors=True)
        run("indicator", "indicator", "-i", src, "--rule", "icpac-onset", "--first-in", f"{w}d",
            "--season-start", "09-15", "--probability", "--time-dim", "step", "-v", var, "-o", by)
        p = xr.open_zarr(by)["probability"].load()
        rows.append(p.isel(step=0).drop_vars("step", errors="ignore"))
    wk = xr.concat(rows, dim=pd.Index(WINDOWS, name="window")).expand_dims(step=[np.timedelta64(0, "ns")])
    wk.attrs = {"units": "1", "long_name": "Share of members whose first onset since 15 Sep falls within the window"}
    fine = tmp / f"live_{model}_weekly.zarr"
    shutil.rmtree(fine, ignore_errors=True)
    wk.to_dataset(name="probability").to_zarr(fine, mode="w")
    out = WORK / "live" / f"{model}_wk.zarr"
    shutil.rmtree(out, ignore_errors=True)
    if model == "gefs":
        wk = wk.where(wk["window"] == 7)  # 35-day GEFS decides the 7-day window only (members_predictors.py)
        wk.to_dataset(name="probability").to_zarr(fine, mode="w")
    if model in ("ecmwf", "gefs"):  # already on the 1.5 deg cells
        run("clip-region", "clip", "-i", fine, "--bbox", BOX, "-o", out)
    else:
        run("coarsen", "coarsen", "-i", fine, "--reference-grid", WORK / "grid.zarr", "--method", "conservative", "-o", out)
    q = xr.open_zarr(out)["probability"]
    print(f"live {model} {INIT}: {dict(q.sizes)}, mean by window {q.mean(['latitude', 'longitude']).values.round(3)}")

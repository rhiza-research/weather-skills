"""Live member predictors for one init, built the same way as the hindcast ones (members_predictors.py).

Usage: python members_live.py ARCHIVE WORK INIT [MODEL ...]      e.g. ... 2026-10-15 ecmwf cumulus

For each model: indicator --rule icpac-onset --first-in 7d --season-start 09-15 --probability
--time-dim step on ARCHIVE/live/MODEL/MODEL_INIT.zarr, read at the start of lead weeks 1-2 (the
share of members whose first onset since 15 Sep falls in that week), put on WORK/grid.zarr (clip-region for ECMWF's 1.5 deg grid, coarsen for Cumulus's 1 deg).
Writes WORK/live/MODEL_wk.zarr.
"""

import shutil
import sys
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

ARCHIVE, WORK, INIT = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
MODELS = sys.argv[4:] or ["ecmwf", "cumulus"]
WEEKS = np.array([0, 7])
BOX = "8.25/27.75/-6.75/48.75"
tmp = WORK / "tmp"
(WORK / "live").mkdir(parents=True, exist_ok=True)
tmp.mkdir(exist_ok=True)

for model in MODELS:
    src = ARCHIVE / "live" / model / f"{model}_{INIT}.zarr"
    var = list(xr.open_zarr(src).data_vars)[0]
    by = tmp / f"live_{model}_by_day.zarr"
    shutil.rmtree(by, ignore_errors=True)
    run("indicator", "indicator", "-i", src, "--rule", "icpac-onset", "--first-in", "7d",
        "--season-start", "09-15", "--probability", "--time-dim", "step", "-v", var, "-o", by)
    p = xr.open_zarr(by)["probability"].load()
    days = (p.step.values / np.timedelta64(1, "D")).astype(int)
    rows = [p.isel(step=int(np.flatnonzero(days == w)[0])).drop_vars("step", errors="ignore") for w in WEEKS]
    wk = xr.concat(rows, dim="step").assign_coords(step=WEEKS.astype("timedelta64[D]").astype("timedelta64[ns]"))
    wk.attrs = {"units": "1", "aggregation_period": "7 day",
                "long_name": "Share of members whose first onset since 15 Sep falls in the week"}
    fine = tmp / f"live_{model}_weekly.zarr"
    shutil.rmtree(fine, ignore_errors=True)
    wk.to_dataset(name="probability").to_zarr(fine, mode="w")
    out = WORK / "live" / f"{model}_wk.zarr"
    shutil.rmtree(out, ignore_errors=True)
    if model == "ecmwf":
        run("clip-region", "clip", "-i", fine, "--bbox", BOX, "-o", out)
    else:
        run("coarsen", "coarsen", "-i", fine, "--reference-grid", WORK / "grid.zarr", "--method", "conservative", "-o", out)
    q = xr.open_zarr(out)["probability"]
    print(f"live {model} {INIT}: {dict(q.sizes)}, mean by week {q.mean(['latitude', 'longitude']).values.round(3)}")

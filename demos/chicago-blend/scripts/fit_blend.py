"""Fit blend weights on all hindcast seasons and blend the live forecast, for each predictor set.

Usage: python fit_blend.py WORK SET [SET ...]      e.g.  fit_blend.py $WORK ecmwf+gefs cumulus

Each SET names forecasts joined by '+'. For a forecast NAME it reads the hindcast
predictors WORK/NAME_hind.zarr and the live forecast WORK/live/NAME_wk.zarr, fits
logistic weights per window against WORK/obs_event.zarr (mme-fit), and blends
the live forecast (mme-blend). Writes WORK/w_SET.zarr and WORK/p2026_SET.zarr.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent)
)  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

W = Path(sys.argv[1])
for cfg in sys.argv[2:]:
    names = cfg.split("+")
    hind = [a for n in names for a in ("--forecast", W / f"{n}_hind.zarr")]
    live = [a for n in names for a in ("--forecast", W / "live" / f"{n}_wk.zarr")]
    run(
        "mme-fit",
        "mme_fit",
        *hind,
        "--names",
        ",".join(names),
        "--obs",
        W / "obs_event.zarr",
        "--model",
        "logistic",
        "--fit-along",
        "window",
        "-o",
        W / f"w_{cfg}.zarr",
    )
    run(
        "mme-blend",
        "mme_blend",
        *live,
        "--names",
        ",".join(names),
        "--weights",
        W / f"w_{cfg}.zarr",
        "-o",
        W / f"p2026_{cfg}.zarr",
    )
    print(f"fit and blended {cfg}")

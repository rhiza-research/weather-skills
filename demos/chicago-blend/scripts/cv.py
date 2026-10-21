"""Leave-one-season-out test of mme-fit / mme-blend for the first OND onset within 7 / 14 days of each init.

Usage: python cv.py WORK [no-gefs | cumulus | cumulus-no-gefs]

For each held-out season: fit logistic weights per window on the other
seasons with the mme-fit skill, blend the held-out season's forecasts with
mme-blend, and score against the observed first onset within the window (Brier score).
Every predictor set is scored on the same inits: those every input has. With
`cumulus`, WORK/cumulus_hind.zarr joins the inputs, which limits the inits to the
seasons Cumulus, ECMWF and GEFS share; `cumulus-no-gefs` drops GEFS, so the
seasons are the ones ECMWF and Cumulus share; `no-gefs` scores ECMWF alone. Scores go to cv_predictions_<mode>.csv.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(
    0, str(Path(__file__).parent)
)  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

D = Path(sys.argv[1])
FOLD = D / "cv"
FOLD.mkdir(exist_ok=True)
MODE = sys.argv[2] if len(sys.argv) > 2 else ""
CUMULUS = MODE in ("cumulus", "cumulus-no-gefs")
CONFIGS = {"models": ["ecmwf", "gefs"], "clim": ["clim"], "models+clim": ["ecmwf", "gefs", "clim"]}
SRC = {"ecmwf": D / "ecmwf_hind.zarr", "gefs": D / "gefs_hind.zarr", "clim": D / "clim_hind.zarr"}
if CUMULUS:
    CONFIGS = {
        "ecmwf+gefs": ["ecmwf", "gefs"],
        "ecmwf+gefs+cumulus": ["ecmwf", "gefs", "cumulus"],
        "ecmwf+cumulus": ["ecmwf", "cumulus"],
        "cumulus": ["cumulus"],
        "clim": ["clim"],
    }
    SRC["cumulus"] = D / "cumulus_hind.zarr"
if MODE == "cumulus-no-gefs":
    # Without GEFS (whose reforecast ends in 2019) the shared seasons run as far as ECMWF and Cumulus do.
    CONFIGS = {
        "ecmwf": ["ecmwf"],
        "ecmwf+cumulus": ["ecmwf", "cumulus"],
        "cumulus": ["cumulus"],
        "clim": ["clim"],
    }
    del SRC["gefs"]
if MODE == "no-gefs":
    CONFIGS = {"ecmwf": ["ecmwf"], "ecmwf+clim": ["ecmwf", "clim"], "clim": ["clim"]}
    del SRC["gefs"]


data = {k: xr.open_zarr(p).load() for k, p in SRC.items()}
common = sorted(set.intersection(*(set(pd.DatetimeIndex(d.time.values)) for d in data.values())))
years = sorted({t.year for t in common})
print(f"{len(common)} common inits in {len(years)} seasons: {years[0]}-{years[-1]}", flush=True)
obs = xr.open_zarr(D / "obs_event.zarr").load()["indicator"]

rows = []
for y in years:
    test = [t for t in common if t.year == y]
    train = [t for t in common if t.year != y]
    for name, ds in data.items():
        ds.sel(time=train).to_zarr(FOLD / f"{name}_train.zarr", mode="w", consolidated=True)
        ds.sel(time=test).to_zarr(FOLD / f"{name}_test.zarr", mode="w", consolidated=True)
    for cfg, names in CONFIGS.items():
        fc = [a for n in names for a in ("--forecast", FOLD / f"{n}_train.zarr")]
        run(
            "mme-fit",
            "mme_fit",
            *fc,
            "--names",
            ",".join(names),
            "--obs",
            D / "obs_event.zarr",
            "--model",
            "logistic",
            "--fit-along",
            "window",
            "-o",
            FOLD / "w.zarr",
        )
        fc = [a for n in names for a in ("--forecast", FOLD / f"{n}_test.zarr")]
        run(
            "mme-blend",
            "mme_blend",
            *fc,
            "--names",
            ",".join(names),
            "--weights",
            FOLD / "w.zarr",
            "-o",
            FOLD / "p.zarr",
        )
        p = xr.open_zarr(FOLD / "p.zarr").load()["probability"]
        for t in test:
            step = p.step.values[0]  # 0: every window starts at the init
            valid = (t + pd.Timedelta(step)).normalize()
            for win in p.window.values:
                # Match cells by coordinate, not array position: the inputs are not all stored
                # in the same latitude order (CHIRPS and Cumulus north-first, ECMWF south-first).
                pr_da = p.sel(time=t, step=step, window=win).transpose("latitude", "longitude")
                cells = dict(latitude=pr_da.latitude, longitude=pr_da.longitude)
                if valid in obs.indexes["time"]:
                    o = obs.sel(time=valid, window=win).sel(cells).transpose("latitude", "longitude").values
                else:
                    o = np.full(pr_da.shape, np.nan)
                pr = pr_da.values
                cl = (
                    data["clim"]["probability"]
                    .sel(time=t, step=step, window=win)
                    .sel(cells)
                    .transpose("latitude", "longitude")
                    .values
                )
                # Score every set on the same samples: cells where every input is present.
                m = np.isfinite(o) & np.isfinite(pr) & np.isfinite(cl)
                for d in data.values():
                    m &= np.isfinite(
                        d["probability"].sel(time=t, step=step, window=win).sel(cells).transpose("latitude", "longitude").values
                    )
                lat, lon = np.meshgrid(pr_da.latitude.values, pr_da.longitude.values, indexing="ij")
                rows += [
                    dict(year=y, window=int(win), cfg=cfg, latitude=la, longitude=lo, p=a, clim=c, obs=b)
                    for la, lo, a, c, b in zip(lat[m], lon[m], pr[m], cl[m], o[m], strict=True)
                ]
    print(f"season {y} done", flush=True)

df = pd.DataFrame(rows)
df.to_csv(D / (f"cv_predictions_{MODE}.csv" if MODE else "cv_predictions.csv"), index=False)
out = []
for (cfg, window), g in df.groupby(["cfg", "window"]):
    bs = ((g.p - g.obs) ** 2).mean()
    bs_clim = ((g.clim - g.obs) ** 2).mean()
    bs_base = ((g.obs.mean() - g.obs) ** 2).mean()
    out.append(
        dict(
            cfg=cfg,
            window=window,
            n=len(g),
            events=int(g.obs.sum()),
            brier=round(bs, 4),
            bss_vs_clim=round(1 - bs / bs_clim, 3),
            bss_vs_base_rate=round(1 - bs / bs_base, 3),
        )
    )
print(pd.DataFrame(out).to_string(index=False))

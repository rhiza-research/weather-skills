"""Write the member-blend report as Markdown.

Usage: python report.py OUT_MD RUN_DIR

RUN_DIR holds the run_demo.sh outputs: cv_predictions_cumulus-no-gefs.csv (cv.py), w_SET.zarr and
p2026_SET.zarr (fit_blend.py, masked by started.py), clim_2026_analog.zarr and clim_2026_all.zarr
(clim.py, masked by started.py), started.zarr, cells_<country>.zarr and init.txt.
Maps are expected next to OUT_MD as map_<set>.png.
"""

import sys
from pathlib import Path

import pandas as pd
import xarray as xr

out_md, run = Path(sys.argv[1]), Path(sys.argv[2])
init = (run / "init.txt").read_text().strip()
WINDOWS = [7, 14]  # days from the init
WEEKS = [f"By {pd.Timestamp(init) + pd.Timedelta(days=w - 1):%-d %b}" for w in WINDOWS]
SETS = {"ecmwf": "ECMWF", "cumulus": "Cumulus", "ecmwf+cumulus": "ECMWF + Cumulus"}


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(map(str, r)) + " |" for r in rows]
    return "\n".join(lines)


df = pd.read_csv(run / "cv_predictions_cumulus-no-gefs.csv")
years = sorted(df.year.unique())
REGIONS = {"All cells": None}
for country in ("Kenya", "Uganda"):
    ds = xr.open_zarr(run / f"cells_{country}.zarr")
    v = ds[next(iter(ds.data_vars))].load()
    v = v.isel({d: 0 for d in v.dims if d not in ("latitude", "longitude")})
    REGIONS[country] = v.notnull()


def in_region(lat, lon, mask):
    """True for the (lat, lon) cells inside mask (None: every cell)."""
    if mask is None:
        return pd.Series(True, index=lat.index)
    inside = {(round(float(a), 2), round(float(b), 2)) for a in mask.latitude.values
              for b in mask.longitude.values if mask.sel(latitude=a, longitude=b)}
    return pd.Series([(round(a, 2), round(b, 2)) in inside for a, b in zip(lat, lon, strict=True)], index=lat.index)


def skill_section(mask):
    d = df[in_region(df.latitude, df.longitude, mask)]
    rows = []
    for cfg, label in SETS.items():
        row = [label]
        for win in WINDOWS:
            g = d[(d.cfg == cfg) & (d.window == win)]
            row.append(f"{1 - ((g.p - g.obs) ** 2).mean() / ((g.clim - g.obs) ** 2).mean():+.2f}")
        rows.append(row)
    first = d[d.cfg == "ecmwf"]
    events = [int(first[first.window == w].obs.sum()) for w in WINDOWS]
    samples = [int((first.window == w).sum()) for w in WINDOWS]
    return rows, events, samples


def outlook_section(mask):
    rows = []
    for name, label in [(f"p2026_{c}", f"{l} blend") for c, l in SETS.items()] + [
        ("clim_2026_analog", "Climatology, El Niño analog years"),
        ("clim_2026_all", "Climatology, all years 1981-2025"),
    ]:
        p = xr.open_zarr(run / f"{name}.zarr")["probability"].squeeze(drop=True).load()
        if mask is not None:
            p = p.where(mask.reindex_like(p, method="nearest", tolerance=0.01).fillna(False))
        means = p.mean(["latitude", "longitude"]).values
        rows.append([label] + [f"{v:.0%}" for v in means])
    return rows


weights = []
for cfg, label in SETS.items():
    w = xr.open_zarr(run / f"w_{cfg}.zarr").load()
    for k, week in enumerate(WEEKS):
        coefs = ", ".join(f"{SETS[p]} {w.coef.isel(window=k).sel(predictor=p).item():.2f}" for p in w.predictor.values)
        weights.append([label, f"{WINDOWS[k]} days", f"{w.intercept.isel(window=k).item():.2f}", coefs])

started = xr.open_zarr(run / "started.zarr")["indicator"].sel(time=pd.Timestamp(init) - pd.Timedelta(days=1)).load() == 1
skill_md, outlook_md = [], []
for region, mask in REGIONS.items():
    rows, events, samples = skill_section(mask)
    cells = "" if mask is None else f" ({int(mask.sum())} cells)"
    skill_md.append(f"""**{region}**{cells}: {samples[0]:,} samples, {events[0]} / {events[1]} onsets within 7 / 14 days.

{table(["Predictors"] + WEEKS, rows)}
""")
    here = started if mask is None else started.where(mask.reindex_like(started, method="nearest", tolerance=0.01).fillna(False), False)
    outlook_md.append(f"""**{region}**: rains already started (CHIRPS) in {int(here.sum())} cells.

{table(["Forecast"] + WEEKS, outlook_section(mask))}
""")
skill_md, outlook_md = "\n".join(skill_md), "\n".join(outlook_md)

md = f"""# Have the rains started? Blended onset forecast, OND 2026

**Event:** the season's first ICPAC rainy-season onset (on or after 15 September) comes within 7 / 14
days of the forecast date: the chance the rains have started by then, where they had not by the
forecast date. **Region:** Ethiopia south of 8°N, Kenya and Uganda, 1.5° cells. **Truth:** CHIRPS.
**Predictors:** for each model, the share of its ensemble members whose first onset comes within the window.
**Method:** for each window, a logistic regression (`mme-fit --model logistic --fit-along window`) turns
the members' onset share into a calibrated probability; `mme-blend` applies it to the live forecast.
Only 14 days ahead: the onset rule looks 27 days past the first rains, so a 46-day forecast can decide
it only that far.

## How well does it work? (leave-one-season-out cross-validation)
Brier skill score against the climatological probability: above 0 beats climatology, 1 is perfect.
{len(years)} seasons ({years[0]}–{years[-1]}), the years ECMWF and Cumulus share; within each region every
set is scored on the same samples (init × cell). Kenya and Uganda each count every 1.5° cell that touches them, so
border cells count for both.

{skill_md}

## Blend weights (fitted on all seasons; log-odds per unit of members' onset share)

{table(["Blend", "Window", "Intercept", "Coefficients"], weights)}

## 2026 outlook (init {init})
Area-mean chance that the rains have started by each date. Cells where CHIRPS already shows the
onset before {init} count as 1:

{outlook_md}
![ECMWF + Cumulus](map_ecmwf+cumulus.png)
![Kenya, ECMWF + Cumulus](map_ecmwf+cumulus_Kenya.png)
![Uganda, ECMWF + Cumulus](map_ecmwf+cumulus_Uganda.png)
![Cumulus](map_cumulus.png)
![ECMWF](map_ecmwf.png)

## Caveats
- {len(years)} seasons of Cumulus hindcasts: one unusual season can change the ranking.
- Live ECMWF is the current model cycle (101 members); its weights come from the 2022-cycle
  reforecast (10 members), whose member shares are coarser.
- An onset is confirmed only 21 days after its first rains, so CHIRPS can mark only the onsets that
  began by about three weeks before {init}. Places whose rains began after that are still shown, and
  their chance is the blend's "if not started yet" answer.
"""
out_md.write_text(md)
print(f"wrote {out_md}")

"""Write the member-blend report as Markdown.

Usage: python report.py OUT_MD RUN_DIR

RUN_DIR holds the run_demo.sh outputs: cv_predictions_cumulus-no-gefs.csv (cv.py), w_SET.zarr and
p2026_SET.zarr (fit_blend.py), clim_2026_analog.zarr and clim_2026_all.zarr (clim.py) and init.txt.
Maps are expected next to OUT_MD as map_<set>.png.
"""

import sys
from pathlib import Path

import pandas as pd
import xarray as xr

out_md, run = Path(sys.argv[1]), Path(sys.argv[2])
WEEKS = ["Week 1", "Week 2"]
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
        for week in range(1, len(WEEKS) + 1):
            g = d[(d.cfg == cfg) & (d.week == week)]
            row.append(f"{1 - ((g.p - g.obs) ** 2).mean() / ((g.clim - g.obs) ** 2).mean():+.2f}")
        rows.append(row)
    first = d[d.cfg == "ecmwf"]
    events = [int(first[first.week == w].obs.sum()) for w in range(1, len(WEEKS) + 1)]
    samples = [int((first.week == w).sum()) for w in range(1, len(WEEKS) + 1)]
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
        rows.append([label] + [f"{v:.0%}" for v in means] + [f"{float(p.max()):.0%}"])
    return rows


weights = []
for cfg, label in SETS.items():
    w = xr.open_zarr(run / f"w_{cfg}.zarr").load()
    for k, week in enumerate(WEEKS):
        coefs = ", ".join(f"{SETS[p]} {w.coef.isel(step=k).sel(predictor=p).item():.2f}" for p in w.predictor.values)
        weights.append([label, week, f"{w.intercept.isel(step=k).item():.2f}", coefs])

skill_md, outlook_md = [], []
for region, mask in REGIONS.items():
    rows, events, samples = skill_section(mask)
    cells = "" if mask is None else f" ({int(mask.sum())} cells)"
    skill_md.append(f"""**{region}**{cells}: {samples[0]:,} / {samples[1]:,} cell-weeks, {events[0]} / {events[1]} onsets in weeks 1 / 2.

{table(["Predictors"] + WEEKS, rows)}
""")
    outlook_md.append(f"""**{region}**

{table(["Forecast"] + WEEKS + ["Highest cell"], outlook_section(mask))}
""")
skill_md, outlook_md = "\n".join(skill_md), "\n".join(outlook_md)

init = (run / "init.txt").read_text().strip()
md = f"""# Will the rains start this week? Blended onset forecast, OND 2026

**Event:** the ICPAC rainy-season onset falls in the lead week (the first onset on or after 15 September).
**Region:** Ethiopia south of 8°N, Kenya and Uganda, 1.5° cells. **Truth:** CHIRPS.
**Predictors:** for each model, the share of its ensemble members whose first onset falls in the week.
**Method:** for each lead week, a logistic regression (`mme-fit --model logistic --fit-along step`) turns
the members' onset share into a calibrated probability; `mme-blend` applies it to the live forecast.
Only weeks 1-2: the onset rule looks 27 days ahead, so a 46-day forecast can decide it only that far.

## How well does it work? (leave-one-season-out cross-validation)
Brier skill score against the climatological probability: above 0 beats climatology, 1 is perfect.
{len(years)} seasons ({years[0]}–{years[-1]}), the years ECMWF and Cumulus share; within each region every
set is scored on the same cell-weeks. Kenya and Uganda each count every 1.5° cell that touches them, so
border cells count for both.

{skill_md}

## Blend weights (fitted on all seasons; log-odds per unit of members' onset share)

{table(["Blend", "Lead", "Intercept", "Coefficients"], weights)}

## 2026 outlook (init {init})
Area-mean probability that the rains start in each lead week, and the highest cell:

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
- Where the rains have already started before the init, the members' share is 0 and the blend gives
  its lowest probability; those cells are not masked on the maps.
"""
out_md.write_text(md)
print(f"wrote {out_md}")

"""Write the evaluation report for one event as Markdown.

Usage: python report.py OUT_MD TITLE RUN_DIR

RUN_DIR holds main/ (ECMWF + GEFS, 2019 model cycle, 20 seasons) and c2022/
(ECMWF + Cumulus, 2022 model cycle, 2016-2021), each after events.py, clim.py,
cv.py and fit_blend.py. Maps are expected next to OUT_MD as map_<set>.png.
"""

import sys
from pathlib import Path

import pandas as pd
import xarray as xr

out_md, title, run_dir = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
main, c22 = run_dir / "main", run_dir / "c2022"
WEEKS = ["Week 1", "Week 2"]


def skill_table(csv, sets):
    """Brier skill score against the base rate, per set and lead week."""
    df = pd.read_csv(csv)
    rows = []
    for cfg in sets:
        g = df[df.cfg == cfg]
        bss = []
        for week in range(1, len(WEEKS) + 1):
            x = g[g.week == week]
            base = ((x.obs.mean() - x.obs) ** 2).mean()
            bss.append(f"{1 - ((x.p - x.obs) ** 2).mean() / base:+.2f}")
        rows.append([cfg] + bss)
    events = [int(df[(df.cfg == sets[0]) & (df.week == w)].obs.sum()) for w in range(1, len(WEEKS) + 1)]
    return rows, events, sorted(df.year.unique())


def weights_rows(path):
    w = xr.open_zarr(path).load()
    rows = []
    for k in range(w.sizes["step"]):
        coefs = ", ".join(
            f"{p} {w.coef.isel(step=k).sel(predictor=p).item():.3f}" for p in w.predictor.values
        )
        rows.append([WEEKS[k], f"{w.intercept.isel(step=k).item():.2f}", coefs])
    return rows


def outlook_row(path, label):
    p = xr.open_zarr(path)["probability"].squeeze(drop=True).load()
    means = p.mean(["latitude", "longitude"]).values
    return [label] + [f"{v:.2f}" for v in means] + [f"{float(p.min()):.2f}–{float(p.max()):.2f}"]


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(map(str, r)) + " |" for r in rows]
    return "\n".join(lines)


main_rows, main_events, main_years = skill_table(main / "cv_predictions.csv", ["models", "clim"])
c22_rows, c22_events, c22_years = skill_table(
    c22 / "cv_predictions_cumulus-no-gefs.csv", ["cumulus", "ecmwf+cumulus", "ecmwf"]
)
best = max(main_rows + c22_rows, key=lambda r: float(r[1]))

md = f"""# {title}

**Event:** {
    (main / "event.txt").read_text().strip()
}. **Region:** Kenya, 7 × 7 cells at 1.5°. **Truth:** CHIRPS.
**Method:** for each lead week, a logistic regression (`mme-fit --model logistic --fit-along step`) turns each
model's member onset probability into the probability of the event; `mme-blend` applies it to the live forecast.

## How well does it work? (leave-one-season-out cross-validation)
Brier skill score against always forecasting the base rate: above 0 beats it, 1 is perfect.

**ECMWF + GEFS**, {len(main_years)} OND seasons ({main_years[0]}–{
    main_years[-1]
}); events per lead week {main_events}:

{
    table(
        ["Predictors"] + WEEKS,
        [["ECMWF + GEFS" if r[0] == "models" else "Climatology", *r[1:]] for r in main_rows],
    )
}

**With Cumulus**, {len(c22_years)} OND seasons ({c22_years[0]}–{
    c22_years[-1]
}), the years ECMWF and Cumulus share; events per lead week {c22_events}:

{
    table(
        ["Predictors"] + WEEKS,
        [
            [
                r[0].replace("+", " + ").replace("ecmwf", "ECMWF").replace("cumulus", "Cumulus"),
                *r[1:],
            ]
            for r in c22_rows
        ],
    )
}

Best week-1 skill: **{best[0]} ({best[1]})**.

## Blend weights (fitted on all seasons; log-odds per unit of member onset probability)
**ECMWF + GEFS:**

{table(["Lead", "Intercept", "Coefficients"], weights_rows(main / "w_ecmwf+gefs.zarr"))}

**Cumulus:**

{table(["Lead", "Intercept", "Coefficients"], weights_rows(c22 / "w_cumulus.zarr"))}

## 2026 outlook (init {(main / "init.txt").read_text().strip()})
Area-mean probability of the event in each lead week, and the range over cells:

{
    table(
        ["Blend"] + WEEKS + ["Cell range"],
        [
            outlook_row(main / "p2026_ecmwf+gefs.zarr", "ECMWF + GEFS"),
            outlook_row(c22 / "p2026_cumulus.zarr", "Cumulus"),
            outlook_row(c22 / "p2026_ecmwf+cumulus.zarr", "ECMWF + Cumulus"),
        ],
    )
}

![ECMWF + GEFS](map_ecmwf+gefs.png)
![Cumulus](map_cumulus.png)

## Caveats
- Cumulus overlaps the other hindcasts for only {
    len(c22_years)
} seasons; one unusual season (2019) can change the ranking.
- Live ECMWF is the current model cycle (101 members); its weights come from older hindcasts (11 members).
- Cells where the event may already have happened before the init are not masked.
"""
out_md.write_text(md)
print(f"wrote {out_md}")

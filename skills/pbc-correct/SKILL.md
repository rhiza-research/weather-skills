---
name: pbc-correct
description: "Apply probabilistic bias correction (PBC, Guan et al. 2026) to an ensemble forecast: turn its members into probabilities for the five observed quintiles of a weekly (or other) window, then correct them with Debias++, Persistence++ and Climatology++ trained on past hindcasts and observations. Use to calibrate subseasonal quintile probabilities from any ensemble (ECMWF, NeuralGCM, WeatherNext…), or to put a raw ensemble on the same quintiles for RPS scoring."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/pbc_correct.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# pbc-correct

Calibrates an ensemble forecast's probabilities of landing in each observed quintile. PBC corrects probabilities of events (below each quintile edge), not amounts: the output is five category probabilities that sum to 1, the same `quintile` layout as `pbc-fetch`.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/pbc_correct.py --forecast FC.zarr --hindcast HC.zarr --obs OBS.zarr \
  [--step DAYS] [--components debias,persistence,climatology] [--weights equal|skill] [--years 20] [--margin 35] [--clim-years 10] \
  [-v VAR] -o OUT.zarr
```

### Arguments
- `--forecast` — ensemble to correct: `number` and `step` dims and a `time` init (scalar, or a dim holding several inits), leads already rolled into windows (`aggregate-temporal --period weekly --time-dim step`).
- `--hindcast` — past forecasts from the same model and the same windows: inits stacked on `time` (`concat --dim time`), same grid. Only hindcasts whose target window ended before a forecast's init train it.
- `--obs` — observed window means on the same grid, labeled every day at the window start: daily obs through `aggregate-temporal --window 7 --align left` for weekly windows. Its `aggregation_period` must match the forecast's. Needs `--years` of history before the first init plus the two windows just before each init.
- `--step` — lead in days of the window to correct (e.g. `14` for week 3 of weekly windows from lead 0). Default: the forecast's only step.
- `--components` — corrections to average. Default `debias,persistence,climatology`. `debias,persistence` is the published PBC. `raw` alone writes the uncorrected ensemble on the same quintiles, a single name writes that component alone.
- `--weights` — `equal` (default, as published) averages the components. `skill` fits non-negative weights summing to 1 that minimise the blend's RPS on the training hindcasts, pooled over the region; each training hindcast is corrected from its own past, so the fit is out of sample. A component that adds nothing gets weight 0. It costs one extra correction per training hindcast.
- `--years` — years of hindcasts and observations behind the quintile edges, Debias++ and Persistence++. Default `20`.
- `--margin` — days either side of the target calendar day pooled into training and edges. Default `35`.
- `--clim-years` — most recent years of observations behind Climatology++. Default `10`.
- `--variable`, `-v` — variable in all inputs. Default: each input's first usable variable.
- `--output`, `-o` — output Zarr.

### Method
For a window starting at valid date *v* (init + step), with quintile edges *q*₁…*q*₄ taken from the observations near *v*'s calendar day:
- **raw** — fraction of members at or below each edge.
- **Debias++** — raw plus the mean (observed event − raw probability) over the training hindcasts.
- **Persistence++** — per cell and edge, least squares of the observed event on the raw probability and the observed events in the two windows just before init.
- **Climatology++** — how often the observed window fell below each edge in the last `--clim-years` years: the recent climate, which differs from the 20-year edges when the climate has shifted.

Each is clipped to [0, 1] and made non-decreasing across edges (isotonic regression). The chosen components are averaged with equal weights and made non-decreasing again.

### Output
- The input variable on `time`, `quintile` (0.2, 0.4, 0.6, 0.8, 1.0), latitude, longitude: the probability of each quintile category, units `1`. `time` is the **start of the valid window** (init + step), so the output lines up with the obs for `verify --metric rps`. The init is the coord `forecast_reference_time`.
- Coord `quintile_threshold` (time, quintile, latitude, longitude): the observed upper edge of each category in the obs units, `inf` for the top one.
- Attrs `pbc_components`, `pbc_weights`, `pbc_lead`, and the forecast's `aggregation_period`; with `--weights skill`, `pbc_mean_weights` (mean weight per component over the inits, also printed on stderr).

### Notes
- Quintile edges come from observation windows that ended before the **first** forecast init, within `--years` years, so every init uses the same edges and none uses its own future.
- Observed windows with `aggregation_coverage` below 1 (a missing day) count as missing: never trained on, never used as a lag or for edges.
- A cell with fewer than 10 usable training samples (or 10 Climatology++ windows) stays NaN; stderr lists inits with too few hindcasts. Missing obs are never filled.
- Members and obs are compared in the obs units; give rates on both (mm day-1), not a total against a rate.
- Everything is held in memory: clip and coarsen to the region before fetching 20 years of hindcasts.

### Example
After weekly-rolling an ECMWF forecast and its 20-year hindcast for Kenya, and `aggregate-temporal --window 7 --align left` on CHIRPS coarsened to the forecast grid:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/pbc_correct.py --forecast ecmwf_kenya_weekly.zarr \
  --hindcast ecmwf_kenya_hindcast_weekly.zarr --obs chirps_kenya_7day.zarr --step 14 -o pbc_kenya_week3.zarr
```
Writes PBC-with-Climatology++ quintile probabilities for week 3 over Kenya. Score it with `verify --metric rps`.

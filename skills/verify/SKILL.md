---
name: verify
description: "Verify any dataset against a reference (a forecast against observations, or one product against another) in three calls named by metric: per-sample components (errors, products, event counts, Brier, CRPS, RPS) for every lead, optionally only on dates or cells a mask selects, then, after grouping with summarize-dim, metrics such as RMSE, correlation, ACC, POD, FAR, CSI, balanced error rate, ETS, Heidke, BSS, RPSS, spread/skill, reliability and ROC. Use for forecast skill scores, skill maps, per-region or per-season scores, and event verification."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.10"
  catalog-group: transforms
---

# verify

Verification in three calls, all named by metric: `--mode prepare` writes the per-sample **components** the metrics need (their sufficient statistics), `summarize-dim --method mean` averages them over any time and space groups, and `--mode compute` turns the group means into the **metrics** (RMSE = √mean(mse), not a mean of RMSEs).

## Usage

```bash
# 1. prepare: the components for every --metric, in one file
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --mode prepare --metric NAME [--metric NAME ...] --forecast FC.zarr --obs OBS.zarr [-v NAME] [--threshold T] [--filter MASK.zarr ...] [other inputs below] -o STATS.zarr
# 2. group (summarize-dim skill): the whole file in one call, method mean
summarize_dim.py -i STATS.zarr -o GROUPED.zarr --dim time [--by admin_1] [--lat-weighted] --method mean [--min-coverage 0]
# 3. compute: the same metrics, into one file
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --mode compute --metric NAME [--metric NAME ...] -i GROUPED.zarr -o METRICS.zarr
```

### Metrics
Name the same metrics in prepare and compute. Each metric needs these inputs when preparing:

| `--metric` | Extra input |
|---|---|
| `mae`, `bias`, `mse`, `rmse`, `mape`, `smape`, `pearson`, `fcst`, `obs` | |
| `crps`, `crps_fair` | ensemble (≥ 2 members for `crps_fair`); without members `crps` is `mae` |
| `acc` | `--clim` |
| `pod`, `far`, `pofd`, `csi`, `balanced_error_rate`, `frequency_bias`, `ets` | `--threshold` (optional `--member-fraction`) |
| `brier`, `bss`, `reliability`, `roc` | `--threshold` (optional `--probability`) |
| `heidke` | `--bins` |
| `spread` | ensemble, ≥ 2 members |
| `rps`, `rpss` | quintile forecast (pbc-correct), or ensemble + `--fc-edges` / `--obs-edges` |

A plain mean (`mae`, `bias`, `fcst`) is already the answer after step 2; compute only checks it.

For one number per metric over everything, group over every dim the components have, e.g. `--dim time --dim step --dim latitude --dim longitude` (stations: `--dim time --dim station_id`). Leave out `step` to keep one value per lead.

### Arguments (prepare)
- `--mode prepare`, `--metric` — repeatable; the file holds every component these metrics need, each once, plus `n_valid` (1 on every scored sample).
- `--forecast`, `--obs` — the dataset to score and its reference: any two datasets (a forecast, a satellite product, stations). Bias is forecast − obs; events are counted on the obs.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable.
- `--threshold` — event cutoff, a plain number in the forecast's units: an event is a value ≥ it (`10` on `mm day-1` data means 10 mm per day). Convert other cutoffs yourself first: 50 mm in 3 days on 3-day mean rates is `16.67`. Required by event metrics, refused otherwise.
- `--member-fraction F` — contingency on an ensemble: forecast event when ≥ F of the members reach the threshold. Default: the ensemble mean reaches it.
- `--probability` — the forecast is already the event probability (0–1, no members), e.g. `indicator --probability`. The obs must then be a 0/1 event indicator (`indicator` on the obs) with `--threshold 1`; obs in mm are refused. Default: the share of members ≥ threshold (0 or 1 without members).
- `--clim CLIM.zarr` — climatology on obs dates (`clim-fetch`, `climatology`), for `acc`.
- `--bins E1 E2 ...` — category edges for `heidke`; category k holds values from edge k−1 up to edge k. A value on an edge is in the upper category, here, in `rps` and for `--threshold`.
- `--fc-edges`, `--obs-edges` — for `rps`/`rpss` on an ensemble: category edges with a `quantile` dim (`summarize-dim --method quantile --q 0.333 0.667`, or `climatology --quantiles`), one from the forecast's own climate, one from the obs.
- `--filter MASK.zarr` — repeatable 0/1 mask (an `indicator` output): only samples where every mask is 1 are scored. A mask on `time` follows the forecast leads like the obs; a mask with no time axis (land, a region) holds for every date and lead. Missing mask values leave the sample out.

### Arguments (compute)
- `--mode compute`, `--metric` — repeatable; any metrics the input was prepared for.
- `--input`, `-i` — the grouped components Zarr from summarize-dim.
- `--output`, `-o` — output Zarr (both modes).

### Output
- Prepare: one variable per component. A component is a per-sample sufficient statistic: a value that can be grouped (averaged over any samples) and feed a metric, e.g. `mse` for `rmse`. Components sit on the paired dims (`time`, `step`, grid or stations, plus any other dim such as a `model` dim from `concat`), float32. Errors in the forecast's units (`mse` squared). Attrs `verify_component`, `verify_run`, `verify_config`; the file's `verify_metrics` lists the metrics it was prepared for.
- Every component is NaN on the same samples (missing forecast, member or obs, or filtered out), so group means pool the same samples.
- Compute: every metric's variable(s) in one Zarr, on whatever dims grouping left. `rmse` in the forecast's units, scores unitless. `reliability` gives `forecast_probability`, `observed_frequency`, `sample_fraction` on `probability_bin`; `roc` gives `hit_rate`, `false_alarm_rate` on `probability_threshold` and `roc_area`.

### Notes
- Compute exits 2 when a metric's components are missing (prepare with that `--metric`), when they come from different prepare calls, when they were never grouped, grouped differently (including with and without `--lat-weighted`), or reduced by anything but a mean. A zero denominator (no observed event in a cell for `bss`, `pod`) gives NaN.
- Partial windows (`aggregation_coverage` < 1 on either input) are not scored. Inputs whose `aggregation_period` differs (daily obs against 3-day forecast means) exit 2: aggregate both onto the same windows first.
- Leads are scored directly: a forecast with init `time` and `step` pairs with the obs at init + step; output keeps `(time, step, …)`. Label both on the same bins (after `aggregate-temporal --time-dim step`, obs windows labelled at their first day, `--align left`). Valid times outside the obs record stay NaN.
- Grid spacing must match (exit 2 otherwise): coarsen the obs onto the forecast (`coarsen --reference-grid FC.zarr --method conservative`). Inputs keep only shared labels; no overlap exits 2.
- Obs are converted to the forecast's units; a total against a rate, or units on one side only, exits 2. Offset units (°C) are scored in kelvin; `--threshold` stays in the forecast's °C.
- Deterministic components score the ensemble mean; `brier`, `crps`, `crps_fair`, `ensemble_variance`, `rps` and the reliability/ROC components score the members. A NaN member drops the sample.
- Events on the ensemble mean undercount extremes: averaging members smooths peaks, so the mean rarely reaches a high threshold. For heavy-rain events use `--member-fraction` (e.g. 0.5) or the probabilistic components.
- `far` is the false alarm ratio FP / (TP + FP); `pofd` is the false alarm rate FP / (FP + TN).
- `csi` is TP / (TP + FP + FN). `balanced_error_rate` is the mean of the miss rate FN / (TP + FN) and `pofd`: 0 is perfect, 0.5 is no better than chance, whatever the event's base rate.
- `spread` is √(mean ensemble variance / mean mse of the ensemble mean): 1 is ideal, below 1 under-dispersive, above 1 over-dispersive.
- `pearson` pooled over space and season includes spatial and seasonal variance, so it is not an anomaly correlation; use `acc` with `--clim` for that, or group per cell (`--dim time`). `acc` is uncentred: anomalies from `--clim` are not re-centred on their group mean.
- `smape` is |f − o| / (|f| + |o|), 0–1, without the factor 2. `mape` divides by the obs, so it explodes on dry days; prefer `mae` or `smape` for rainfall.
- To compare models with the same member count, `concat --new-dim model` the forecasts and prepare once. With different member counts (ECMWF 51, GEFS 31) prepare each model separately, then `concat --new-dim model` the components: concatenating the forecasts pads the smaller ensemble with NaN members, which drops all its samples.
- Stations: sample the grid at the station points first, then pass the sample and the station Zarr.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --mode prepare --metric rmse --metric pearson --metric csi \
  --forecast gefs_weekly.zarr --obs chirps_weekly.zarr --threshold 5 -o stats.zarr
summarize_dim.py -i stats.zarr -o by_lead.zarr --dim time --dim latitude --dim longitude --lat-weighted --method mean
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --mode compute --metric rmse --metric pearson --metric csi \
  -i by_lead.zarr -o skill_by_lead.zarr
```
Writes the area-weighted RMSE, correlation and CSI (weeks averaging ≥ 5 mm/day) of past GEFS forecasts (several inits, weekly leads) for each lead.

## Details

### Components
Prepare writes the components the named metrics need. Each is one value per sample; its group mean feeds the metrics.

| Group | Components | Written for | Needs |
|---|---|---|---|
| Errors | `fcst`, `obs`, `bias`, `mae`, `mse`, `mape`, `smape` | the metric of the same name; `rmse` (`mse`) | |
| Moments | `fcst`, `obs`, `squared_fcst`, `squared_obs`, `covariance` | `pearson` | |
| Anomaly moments | `squared_fcst_anom`, `squared_obs_anom`, `anom_covariance` | `acc` | `--clim` |
| Contingency | `true_positives`, `false_positives`, `false_negatives`, `true_negatives` (1 or 0 per sample) | `pod`, `far`, `pofd`, `csi`, `balanced_error_rate`, `frequency_bias`, `ets` | `--threshold` |
| Probability | `brier`, `obs_event` (observed event, 1 or 0) | `brier`, `bss` | `--threshold` |
| Reliability | `reliability_count`, `reliability_fcst`, `reliability_obs` on `probability_bin` (10 bins of 0.1) | `reliability` | `--threshold` |
| ROC | `roc_hits`, `roc_false_alarms` on `probability_threshold` (0.1 … 1.0), with `obs_event` | `roc` | `--threshold` |
| Ensemble | `crps`, `crps_fair`, `ensemble_variance` (with `mse` for `spread`) | `crps`, `crps_fair`, `spread` | members |
| Categories | `n_correct`, `n_fcst_bin`, `n_obs_bin` (the last two on `category`) | `heidke` | `--bins` |
| Ranked probability | `rps`, `rps_clim` | `rps`, `rpss` | quintile forecast, or members + `--fc-edges` / `--obs-edges` |
| Count | `n_valid` (1 on every scored sample) | always | |

The probability, reliability and ROC components use the forecast event probability: the share of members ≥ `--threshold`, or the forecast itself with `--probability`.

### Event filters
Score only dates an obs condition holds, e.g. 30-day rain totals during droughts: run `indicator` on the obs (`--rule "precip sum 30d < 50"`) for the mask, put both inputs on 30-day sums (`aggregate-temporal`), then `--mode prepare --metric mae --filter drought.zarr`. Several `--filter` masks must all hold. For locally extreme days, take the q90 from `climatology --quantiles 0.9`, `difference` the obs minus it, and mask with `indicator "precip >= 0"`.

### Grouping
- With a `--by` time label (`month`, a season) or `--method sum`, pass summarize-dim `--min-coverage 0`: components are NaN on purpose wherever a sample was filtered or missing, and the default 1.0 would drop every such cell. `n_valid` still says how many samples each group pooled.
- Group in one summarize-dim call. Grouping in two steps (time, then space) weights each cell equally, not each sample, so it differs from pooling once wherever cells have different numbers of valid samples.
- Per season: label the components with `label-group --time MAM,OND` (or `month`) and group with `--by` that label and `--min-coverage 0`; `select` the season you want from the result.

### Counts
Group `n_valid` with `summarize-dim -v n_valid --method sum --min-coverage 0` for the number of samples (events, after a filter) behind each group.

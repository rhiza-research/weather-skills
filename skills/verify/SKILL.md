---
name: verify
description: "Verify any dataset against a reference (a forecast against observations, or one product against another) in one call: pair every forecast value and lead with its obs, optionally only on dates or cells a mask selects, pool the samples over chosen dims, regions or seasons, and write metrics such as RMSE, correlation, ACC, POD, FAR, CSI, balanced error rate, ETS, Heidke, BSS, RPSS, spread/skill, reliability and ROC. Use for forecast skill scores, skill maps, per-region or per-season scores, and event verification."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.10"
  catalog-group: transforms
---

# verify

One call scores a forecast against obs. You name the **metrics** and the **grouping**; verify pairs every forecast value with its obs, computes the per-sample **components** each metric needs (its sufficient statistics, such as the squared error for `rmse`), pools them over each group and turns the pooled means into the metrics (RMSE = √mean(squared error), not a mean of RMSEs).

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast FC.zarr --obs OBS.zarr --metric NAME [--metric NAME ...] \
  [--dim DIM ...] [--by LABEL ...] [--lat-weighted] [--threshold T] [--filter MASK.zarr ...] [inputs below] -o METRICS.zarr
```

### Grouping
The metrics keep every dim not pooled. What to pass:

| You want | Grouping flags |
|---|---|
| one number per metric | `--dim` every dim: `--dim time --dim step --dim latitude --dim longitude` (stations: `--dim time --dim station_id`) |
| one value per lead | the same without `--dim step` |
| a skill map (one value per cell) | `--dim time` (and `--dim step` to pool leads) |
| one value per region or season | label `--forecast` with `label-group` first (`--level admin_1`, `--time MAM,OND`), then `--by` that label, with `--dim` for the dims to pool inside each group |
| area-weighted pooling over a grid | add `--lat-weighted` |
| one value per sample | no `--dim` or `--by` |

Every sample in a group counts once: filtered or missing samples are skipped, never filled. `n_valid` in the output is the number of samples behind each value.

### Metrics
Each metric needs these inputs:

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

### Arguments
- `--metric` — repeatable; every metric goes into one output Zarr.
- `--dim` — repeatable; a dim to pool samples over.
- `--by` — repeatable; a label coord (from `label-group`) to pool samples by, one value per label.
- `--lat-weighted` — weigh each grid cell by cos(latitude) when pooling.
- `--forecast`, `--obs` — the dataset to score and its reference: any two datasets (a forecast, a satellite product, stations). Bias is forecast − obs; events are counted on the obs.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable.
- `--threshold` — event cutoff, a plain number in the forecast's units: an event is a value ≥ it (`10` on `mm day-1` data means 10 mm per day). Convert other cutoffs yourself first: 50 mm in 3 days on 3-day mean rates is `16.67`. Required by event metrics, refused otherwise.
- `--member-fraction F` — contingency on an ensemble: forecast event when ≥ F of the members reach the threshold. Default: the ensemble mean reaches it.
- `--probability` — the forecast is already the event probability (0–1, no members), e.g. `indicator --probability`. The obs must then be a 0/1 event indicator (`indicator` on the obs) with `--threshold 1`; obs in mm are refused. Default: the share of members ≥ threshold (0 or 1 without members).
- `--clim CLIM.zarr` — climatology on obs dates (`clim-fetch`, `climatology`), for `acc`.
- `--bins E1 E2 ...` — category edges for `heidke`; category k holds values from edge k−1 up to edge k. A value on an edge is in the upper category, here, in `rps` and for `--threshold`.
- `--fc-edges`, `--obs-edges` — for `rps`/`rpss` on an ensemble: category edges with a `quantile` dim (`summarize-dim --method quantile --q 0.333 0.667`, or `climatology --quantiles`), one from the forecast's own climate, one from the obs.
- `--filter MASK.zarr` — repeatable 0/1 mask (an `indicator` output): only samples where every mask is 1 are scored. A mask on `time` follows the forecast leads like the obs; a mask with no time axis (land, a region) holds for every date and lead. Missing mask values leave the sample out.
- `--output`, `-o` — output Zarr.

### Output
- One variable per metric (several for `reliability` and `roc`), float32, on the dims left after grouping. `rmse`, `mae`, `bias`, `crps` in the forecast's units, `mse` squared, scores unitless. `reliability` gives `forecast_probability`, `observed_frequency`, `sample_fraction` on `probability_bin`; `roc` gives `hit_rate`, `false_alarm_rate` on `probability_threshold` and `roc_area`.
- `n_valid`: the samples pooled into each value.
- Attrs: `cell_methods` (the pooling), `verify_metric`, `verify_config` (inputs and grouping); the file's `verify_metrics` lists the metrics.

### Notes
- A zero denominator (no observed event in a group for `bss`, `pod`) gives NaN.
- Partial windows (`aggregation_coverage` < 1 on either input) are not scored. Inputs whose `aggregation_period` differs (daily obs against 3-day forecast means) exit 2: aggregate both onto the same windows first.
- Leads are scored directly: a forecast with init `time` and `step` pairs with the obs at init + step; output keeps `(time, step, …)`. Label both on the same bins (after `aggregate-temporal --time-dim step`, obs windows labelled at their first day, `--align left`). Valid times outside the obs record stay NaN.
- Grid spacing must match (exit 2 otherwise): coarsen the obs onto the forecast (`coarsen --reference-grid FC.zarr --method conservative`). Inputs keep only shared labels; no overlap exits 2.
- Obs are converted to the forecast's units; a total against a rate, or units on one side only, exits 2. Offset units (°C) are scored in kelvin; `--threshold` stays in the forecast's °C.
- Deterministic metrics score the ensemble mean; `brier`, `bss`, `crps`, `crps_fair`, `spread`, `rps`, `rpss`, `reliability` and `roc` score the members. A NaN member drops the sample.
- Events on the ensemble mean undercount extremes: averaging members smooths peaks, so the mean rarely reaches a high threshold. For heavy-rain events use `--member-fraction` (e.g. 0.5) or the probabilistic metrics.
- `far` is the false alarm ratio FP / (TP + FP); `pofd` is the false alarm rate FP / (FP + TN).
- `csi` is TP / (TP + FP + FN). `balanced_error_rate` is the mean of the miss rate FN / (TP + FN) and `pofd`: 0 is perfect, 0.5 is no better than chance, whatever the event's base rate.
- `spread` is √(mean ensemble variance / mean mse of the ensemble mean): 1 is ideal, below 1 under-dispersive, above 1 over-dispersive.
- `pearson` pooled over space and season includes spatial and seasonal variance, so it is not an anomaly correlation; use `acc` with `--clim` for that, or group per cell (`--dim time`). `acc` is uncentred: anomalies from `--clim` are not re-centred on their group mean.
- `smape` is |f − o| / (|f| + |o|), 0–1, without the factor 2. `mape` divides by the obs, so it explodes on dry days; prefer `mae` or `smape` for rainfall.
- To compare models with the same member count, `concat --new-dim model` the forecasts and verify once; the `model` dim passes through. With different member counts (ECMWF 51, GEFS 31) verify each model separately, then `concat --new-dim model` the outputs: concatenating the forecasts pads the smaller ensemble with NaN members, which drops all its samples.
- Stations: sample the grid at the station points first, then pass the sample and the station Zarr.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast gefs_weekly.zarr --obs chirps_weekly.zarr \
  --metric rmse --metric pearson --metric csi --threshold 5 \
  --dim time --dim latitude --dim longitude --lat-weighted -o skill_by_lead.zarr
```
Writes the area-weighted RMSE, correlation and CSI (weeks averaging ≥ 5 mm/day) of past GEFS forecasts (several inits, weekly leads) for each lead.

## Details

### Components
The metrics are computed from per-sample components, pooled by their mean over each group:

| Group | Components | For | Needs |
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

The probability, reliability and ROC components use the forecast event probability: the share of members ≥ `--threshold`, or the forecast itself with `--probability`.

### Event filters
Score only dates an obs condition holds, e.g. 30-day rain totals during droughts: run `indicator` on the obs (`--rule "precip sum 30d < 50"`) for the mask, put both inputs on 30-day sums (`aggregate-temporal`), then `--metric mae --filter drought.zarr`. Several `--filter` masks must all hold. For locally extreme days, take the q90 from `climatology --quantiles 0.9`, `difference` the obs minus it, and mask with `indicator "precip >= 0"`.

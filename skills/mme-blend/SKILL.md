---
name: mme-blend
description: "Blend several forecasts into one multi-model ensemble (MME) value or event probability using fixed weights given on the command line or fitted weights from a Zarr, as a weighted sum, a linear regression, or a logistic regression. Weights may differ by lead, cell, or station. Use to combine models (and a climatology baseline) into one forecast once weights are chosen."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/mme_blend.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# mme-blend

Combines several forecasts, one predictor each, into one blended value or probability with given weights.

## Usage

```bash
# Fitted weights
uv run ${CLAUDE_SKILL_DIR}/scripts/mme_blend.py --forecast A.zarr --forecast B.zarr [--names A,B] [-v NAME] --weights WEIGHTS.zarr -o OUT.zarr
# Fixed weights
uv run ${CLAUDE_SKILL_DIR}/scripts/mme_blend.py --forecast A.zarr --forecast B.zarr --model weighted|linear|logistic --coef A=W --coef B=W [--intercept B] -o OUT.zarr
```

### Arguments
- `--forecast` — forecast Zarr, one per source (repeat). Each is one predictor.
- `--names` — comma-separated predictor names, one per `--forecast` in order. Default: each file's stem (`aifs.zarr` → `aifs`).
- `--variable`, `-v` — variable read from every forecast. Default: each forecast's first usable variable.
- `--weights` — weights Zarr (format below); its `model` attr picks the model.
- `--model` — `weighted`, `linear`, or `logistic`; required with `--coef`, must match the weights' model with `--weights`.
- `--coef` — `NAME=W` (repeat): a fixed weight per predictor, instead of `--weights`.
- `--intercept` — intercept with `--coef` for `linear` / `logistic`. Default: `0`.
- `--output`, `-o` — output Zarr.

### Output
- `weighted` / `linear`: `blend`, in the weights' `predictor_units` (else the first forecast's units).
- `logistic`: `probability`, 0–1, units `1`.
- The forecasts' dims and coords (init `time`, `step`, grid or stations) are kept; ensemble `number` is averaged first.

### Notes
- Models: `weighted` = `Σ coef × forecast`; `linear` adds `intercept`; `logistic` = `1 / (1 + e^−(Σ coef × forecast + intercept))`.
- Weights on a dim (`step`, `latitude`/`longitude`, `station_id`) pair with the same labels of the forecasts; weights without it apply everywhere (station-fitted pooled weights blend a grid). No shared labels exits 2.
- The forecast names must match the weights' predictors exactly (interaction `a:b` uses `a` and `b`); a mismatch exits 2 with both lists.
- `weighted` / `linear` convert every forecast to one unit; incompatible units exit 2.
- A NaN forecast (or a NaN weight from a failed fit) gives NaN; nothing is filled.
- Weights format, for writing weights from elsewhere: `coef` on dim `predictor` (string labels: forecast names, `a:b` for products) plus any lead / space dims; `intercept` on those dims (absent for `weighted`); dataset attr `model` = `weighted` | `linear` | `logistic`; optional attr `predictor_units`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/mme_blend.py --forecast aifs_onset_prob.zarr --forecast ngcm_onset_prob.zarr \
  --forecast clim_onset_prob.zarr --names aifs,ngcm,clim --weights onset_weights.zarr -o onset_blend.zarr
```
Writes the blended onset probability for each forecast week and grid cell of the latest init, using weights fitted per week.

---
name: mme-fit
description: "Fit multi-model ensemble (MME) blend weights from several hindcast forecasts and a ground truth (gridded or station observations), as a weighted sum, a linear regression, or a logistic regression for event probability, with one fit overall or one per lead, cell, or station. Writes the weights as a Zarr. Use to learn how much to trust each model before blending forecasts, e.g. AI models plus climatology for rainy-season onset."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/mme_fit.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# mme-fit

Fits blend weights that turn several forecasts (one predictor each) into the observed value or event, and writes them as a weights Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/mme_fit.py --forecast A.zarr --forecast B.zarr [--forecast ...] --obs OBS.zarr --model weighted|linear|logistic [--names A,B] [-v NAME] [--obs-variable NAME] [--interaction A:B] [--fit-along DIM ...] [--alpha 0] [--max-iter 100] [--tol 1e-4] -o WEIGHTS.zarr
```

### Arguments
- `--forecast` — forecast Zarr, one per source (repeat): hindcasts with `time` (inits) and `step`, or a single init, or a forecast already on valid dates (no `step`). Each is one predictor.
- `--obs` — ground truth with a `time` axis of valid dates, gridded or `station_id`.
- `--model` — `weighted` (`X·coef`), `linear` (`X·coef + intercept`), or `logistic` (`1 / (1 + e^−(X·coef + intercept))`, a probability).
- `--names` — comma-separated predictor names, one per `--forecast` in order. Default: each file's stem (`aifs.zarr` → `aifs`).
- `--variable`, `-v` — variable read from every forecast. Default: each forecast's first usable variable.
- `--obs-variable` — variable read from `--obs`. Default: its first usable variable.
- `--interaction` — `A:B` (repeat): adds the product of named predictors as one more predictor.
- `--fit-along` — dim that gets its own weights, one fit per entry (repeat), e.g. `step`, `latitude` + `longitude`, `station_id`. Default: none, one fit over every sample.
- `--alpha` — L2 penalty on `coef` (never the intercept). Default: `0`.
- `--max-iter` — logistic Newton iterations. Default: `100`.
- `--tol` — logistic stop when the largest coefficient step is below this. Default: `1e-4`.
- `--output`, `-o` — output weights Zarr.

### Output
- `coef` on `predictor` (forecast names, then interactions `a:b`) plus the `--fit-along` dims.
- `intercept` on the `--fit-along` dims (`linear`, `logistic`; in the obs units for `linear`, the logit for `logistic`).
- `n_samples` (samples with the obs and every predictor present) and, for `logistic`, `n_iter` (`max_iter` = not converged).
- Attrs `model`, `alpha`, `fit_along`, `pooled_dims`, and `predictor_units` (the obs units the forecasts were converted to, `weighted`/`linear`). Coords on the fit dims (station `latitude`, `longitude`, `name`) are kept.
- Stderr prints the coefficients (one fit) or a count of fits.

### Notes
- A sample is one (init, step, cell or station). Every dim not in `--fit-along` is pooled into the samples.
- The obs are read at each sample's valid date `init + step` (steps labelled at the start of their window). Daily obs match on the calendar date, ignoring the init hour. For window forecasts (weekly steps), give obs for the same window starting on every day, e.g. a 7-day rolling window labelled left; differing `aggregation_period` attrs exit 2.
- Space must already match: the same grid spacing, or shared `station_id`. Stations missing from any input are left out and listed. Gridded forecasts against station obs exit 2: sample them at the stations first (point-value with `--points OBS.zarr`).
- A sample missing the obs or any predictor is left out; nothing is filled. `logistic` needs a 0/1 obs (an event mask); its forecasts can be event probabilities or amounts.
- Onset-type events: give the obs as "first event in the window from each date" (indicator `--first-in 7d` on daily obs from the season start); dates after the observed onset are NaN, so inits after onset drop out.
- `weighted` and `linear` convert every forecast to the obs units; incompatible units exit 2.
- Matches scikit-learn: `weighted` = `LinearRegression(fit_intercept=False)`, `linear` = `LinearRegression`, both `Ridge(alpha)` when `--alpha` > 0; `logistic` = `LogisticRegression(C=1/alpha)` (`alpha` 0 is unpenalized, unlike scikit-learn's default `C=1`). Predictors are not rescaled, so `--alpha` depends on their units.
- A fit with no samples or a singular system (collinear predictors) gets NaN weights. A logistic fit that does not converge (an event perfectly separated by the predictors) warns; raise `--alpha`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/mme_fit.py --forecast aifs_onset_prob.zarr --forecast ngcm_onset_prob.zarr \
  --forecast clim_onset_prob.zarr --names aifs,ngcm,clim --obs chirps_onset_event.zarr \
  --model logistic --fit-along step -o onset_weights.zarr
```
Fits, for each forecast week, the logistic weights that turn the AIFS, NeuralGCM and climatology onset probabilities into the probability of observed onset, pooled over every hindcast init and grid cell.

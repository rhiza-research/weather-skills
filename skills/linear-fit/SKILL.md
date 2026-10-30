---
name: linear-fit
description: "Fit one variable of a weather-skills Zarr on another by least squares along a sample dim (y = intercept + slope × x), e.g. observed October rainfall on the forecast across years, and write the fitted line, slope, intercept, R² and a prediction interval. Use for forecast-versus-observation regressions, calibrating a forecast against its hindcasts, or a fit line and confidence band on a scatter plot."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/linear_fit.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# linear-fit

Fits `y = intercept + slope × x` by ordinary least squares along one dim and adds the fit to the dataset.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/linear_fit.py -i IN.zarr -o OUT.zarr --x XVAR --y YVAR --dim DIM [--interval 0.8]
```

### Arguments
- `--input`, `-i` — input Zarr holding both variables, e.g. from merge.
- `--x` — predictor variable (e.g. the forecast).
- `--y` — predicted variable (e.g. the observation).
- `--dim` — the dim whose entries are the samples, e.g. `year`. Must be a dim of both variables.
- `--interval` — probability of the prediction interval, between 0 and 1. Default: `0.8` (10th to 90th percentile).
- `--output`, `-o` — output Zarr.

### Output
The input plus these variables:
- `fit`, `fit_lower`, `fit_upper` — along `--dim`: the fitted y and its prediction interval at every x. Samples where x is present but y is missing (a forecast year with no observation yet) get a value too; that is the prediction.
- `slope`, `intercept`, `r2`, `n`, `residual_std` — one value per fit. `n` counts the samples with both x and y; only those are fitted.

Other dims of x and y (latitude, longitude, stations) are kept: each cell or station gets its own fit. A one-value fit prints `y = a + b * x; R2=…, n=…, residual std=…` to stderr.

`fit`, the bounds, `intercept` and `residual_std` carry y's units; `slope` carries y's units over x's.

### Notes
- The interval is the prediction interval for a new sample, `fit ± t × residual_std × √(1 + 1/n + (x − x̄)² / Σ(x − x̄)²)` with `t` from Student's t at `n − 2` degrees of freedom. It widens away from the mean x.
- Fewer than 2 paired samples, or x with no spread, gives NaN slope and fit; fewer than 3 gives NaN `residual_std` and bounds.
- Input variables named like an output variable exit 2; rename them first with edit-coords.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/linear_fit.py -i oct_forecast_obs.zarr -o oct_fit.zarr --x tp --y precip --dim year
```
Regresses observed October rainfall (`precip`) on the forecast October rainfall (`tp`) across years. `fit_lower` and `fit_upper` hold the 80% range at each year's forecast, including the current year, which has no observation yet.

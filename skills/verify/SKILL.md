---
name: verify
description: "Forecast vs observation verification on a shared grid — hits (event classification), bias, MAE, RMSE, and for ensembles CRPS and Brier score. Scores forecast skill; to only compute an event probability use indicator. Cell-by-cell only: coarsen --obs onto the forecast lat/lon grid first, and align time with step-to-time / aggregate-temporal. The output Zarr is the metric field: plot-verify draws hits, bias and mae; use plot for rmse, crps and brier. Do not coarsen inputs just to draw them; plot with two heatmap traces keeps each dataset on its own grid."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# verify

Cell-by-cell forecast verification against observations. Choose a metric
with `--metric`:

| `--metric` | Output variable | Meaning |
| --- | --- | --- |
| `hits` (default) | `event_hit` | Event classification at `--threshold` |
| `bias` | `bias` | `forecast − observation` per cell |
| `mae` | `mae` | `\|forecast − observation\|` per cell |
| `rmse` | `rmse` | `sqrt(mean((forecast − observation)²))` over `--reduce` dims (required) |
| `crps` | `crps` | Ensemble CRPS, `E\|X − y\| − ½E\|X − X′\|` over members (ensemble required); `--crps-estimator standard` (default, M², as properscoring) or `fair` (M(M−1), as WeatherBench 2) |
| `brier` | `brier_score` | `(P(X > threshold) − 1[y > threshold])²`, P = share of members (ensemble + `--threshold` required) |

`--reduce DIM` (repeatable) averages any metric except `hits` over that
dimension, e.g. `--reduce time` for a per-cell score over the period.

**Ensembles.** `crps` and `brier` use every member. The deterministic
metrics (`hits`, `bias`, `mae`, `rmse`) score the **ensemble mean**; the
metric variable records this in its `verify_ensemble_reduction` attribute
(`ds["mae"].attrs`, not the dataset's `ds.attrs`). `crps` and `brier`
refuse a deterministic forecast, and `brier` refuses without an explicit
`--threshold`.

### Hits (`--metric hits`)

An **event** is `--variable` ≥ `--threshold` (default `1`, in stored units):

| Value | Meaning |
| --- | --- |
| `1` (`hit`) | forecast and truth both ≥ threshold |
| `-1` (`disagree`) | one is ≥ threshold and the other is not |
| `0` (`below`) | both below the threshold |

NaNs in either input stay NaN. Ensemble `number` is averaged before
comparison. Inputs are inner-joined (overlapping coordinates only).

Plot hits with `plot` (discrete red / gray / green map). For a lead-week
grid of obs, forecast, and verification maps, use `plot-verify` (it accepts `hits`, `bias`
and `mae`; draw `rmse`, `crps` and `brier_score` fields with `plot`).

## When to use

- Binary event verification (`--metric hits`) — rain ≥ 1 mm, temperature ≥
  35 °C, …
- Continuous error maps (`--metric bias` or `--metric mae`) for forecast
  skill assessment on a shared grid.

**Match obs to the forecast, not the reverse.** Coarsen `--obs` onto the
forecast's lat/lon spacing and offset before this skill. Do not `downscale`
the forecast onto the obs grid. That shared grid is for the subtraction
here, not for drawing the two fields: `plot` with two heatmap traces
keeps each on its own grid. Run `step-to-time` on a classic forecast first. For a precip
threshold in `mm`, run `aggregate-temporal` then `convert-to-totals` first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast <forecast.zarr> --obs <truth.zarr> \
    [--metric hits|bias|mae|rmse|crps|brier] [--variable NAME] [--threshold T] \
    [--reduce DIM ...] \
    -o <verify.zarr>
```

### Arguments

- `--forecast` — forecast Zarr (required).
- `--obs` — truth / observation Zarr (required). Must already be on the
  forecast's spatial resolution.
- `--metric` — `hits`, `bias`, `mae`, `rmse`, `crps`, or `brier` (default `hits`).
- `--variable`, `-v` — data variable in both inputs. Default: each input's
  first usable variable (names may differ).
- `--threshold` — event cutoff. `hits` counts value ≥ threshold (unchanged); `brier`
  counts value > threshold, as Sheerwater's `above_threshold` event and WeatherBench 2 do. `hits` defaults to
  `1`; `brier` requires it; other metrics ignore it.
- `--reduce` — dimension to average the score over (repeatable). Required
  for `rmse`; not allowed for `hits`.
- `--output`, `-o` — output Zarr.

### Output

One verification data variable (`event_hit`, `bias`, or `mae`). Hits output
carries CF `flag_values` `-1, 0, 1` and `flag_meanings`
`disagree below hit`. A regional score is printed to stdout (hit rate for
hits; cos-lat weighted mean for bias/mae).

## Examples

```bash
# Event hits Zarr for re-plotting
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast /tmp/s2s_weekly.zarr --obs /tmp/chirps_weekly.zarr \
    --metric hits --variable precip --threshold 1 -o /tmp/hits.zarr
uv run skills/plot/scripts/plot.py -i /tmp/hits.zarr -o /tmp/hits.png \
    --spec '{"title":"Weekly rain ≥ 1 mm"}'

# Bias error field
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast /tmp/s2s_weekly.zarr --obs /tmp/chirps_weekly.zarr \
    --metric bias --variable precip -o /tmp/bias.zarr
```

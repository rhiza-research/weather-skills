---
name: verify
description: "Forecast vs observation verification on a shared grid or shared stations — hits (event classification), bias (forecast − obs), or MAE (|forecast − obs|). Cell-by-cell only: put fine obs on the coarse forecast grid first with coarsen --reference-grid <forecast.zarr> --method conservative (area mean; linear would point-sample), and align time with step-to-time / aggregate-temporal. For stations, run point-value on the grid first; then verify pairs station by station. The output Zarr is the metric field to draw. Do not coarsen inputs just to draw them; a map can keep each dataset on its own grid."
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

### Hits (`--metric hits`)

An **event** is `--variable` ≥ `--threshold` (default `1`, in the forecast's units):

| Value | Meaning |
| --- | --- |
| `1` (`hit`) | forecast and truth both ≥ threshold |
| `-1` (`disagree`) | one is ≥ threshold and the other is not |
| `0` (`below`) | both below the threshold |

### Alignment, ensembles, NaNs (all metrics)

- Inputs are inner-joined (overlapping coordinates only); no overlap on a dim
  is an error. A scalar `time` (after `select`) is checked the same way, so
  two single-time inputs must carry the same date.
- A forecast with a `step` dim is refused unless `--obs` has `step` too — run
  `step-to-time` first.
- Ensemble `number` is averaged before scoring: every metric scores the
  ensemble mean, not per member. A NaN member leaves that cell NaN.
- NaNs in either input stay NaN and are left out of the printed score (a NaN
  cell is neither an event nor a miss).

### Units

`--obs` is converted to the forecast's units before any comparison (K
observations against a °C forecast, `mm hr-1` against `mm day-1`), so
`--threshold` is always in the forecast's units. Incompatible units — a
weekly total in `mm` against a rate in `mm day-1` — exit non-zero; put both on
one basis with `aggregate-temporal` / `convert-to-totals` first. `bias` and
`mae` carry the forecast's units (a temperature error is `delta_degC`).
Units on only one input exit non-zero; neither having units is allowed.

## When to use

- Binary event verification (`--metric hits`) — rain ≥ 1 mm, temperature ≥
  35 °C, …
- Continuous error maps (`--metric bias` or `--metric mae`) for forecast
  skill assessment on a shared grid.

**Match obs to the forecast, not the reverse.** Coarsen `--obs` onto the
forecast's lat/lon spacing and offset before this skill, with
`coarsen --reference-grid <forecast.zarr> --method conservative` when the
obs are much finer (CHIRPS 0.05° vs a 1.5° forecast): that averages every
obs cell inside the forecast cell, while the default linear method samples
the few cells next to its centre. Do not `downscale` the forecast onto the
obs grid. That shared grid is for the subtraction
here, not for drawing the two fields: a map can keep each on its own
grid. Run `step-to-time` on a classic forecast first. For a precip
threshold in `mm`, run `aggregate-temporal` then `convert-to-totals` first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast <forecast.zarr> --obs <truth.zarr> \
    [--metric hits|bias|mae] [--variable NAME] [--threshold 1] \
    -o <verify.zarr>
```

### Arguments

- `--forecast` — forecast Zarr (required).
- `--obs` — truth / observation Zarr (required). Must already be on the
  forecast's spatial resolution.
- `--metric` — `hits`, `bias`, or `mae` (default `hits`).
- `--variable`, `-v` — data variable in both inputs. Default: each input's
  first usable variable (names may differ).
- `--threshold` — event cutoff for `--metric hits` only (default `1`).
- `--output`, `-o` — output Zarr.

### Station data

To verify a grid against stations, sample the grid at the stations with
`point-value --points <stations.zarr>` first, then pass the sampled Zarr and
the station Zarr as `--forecast` / `--obs` (same variable name and units;
`edit-coords` / `unit-convert` if not). Pairs inner-join on `station_id` and
`time`, so only days both report are scored. The printed score is the plain
mean over every station and time with both values (there is no latitude
grid to weight). Do not pass a raw grid with a station Zarr: the two have no
shared lat/lon labels.

### Output

One verification data variable (`event_hit`, `bias`, or `mae`). Hits output
carries CF `flag_values` `-1, 0, 1` and `flag_meanings`
`disagree below hit`. Bias and MAE keep the input `units`. A regional score
is printed to stdout (hit rate = hits / obs events for hits; cos-lat weighted mean for bias/mae
on a grid, plain mean on stations).

If the inputs share no labels on some dim, the error names that dim and
both ranges (e.g. weekly obs labelled one day off the forecast weeks).

## Examples

```bash
# Event hits Zarr for re-plotting
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast /tmp/s2s_weekly.zarr --obs /tmp/chirps_weekly.zarr \
    --metric hits --variable precip --threshold 1 -o /tmp/hits.zarr

# Bias error field
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast /tmp/s2s_weekly.zarr --obs /tmp/chirps_weekly.zarr \
    --metric bias --variable precip -o /tmp/bias.zarr
```

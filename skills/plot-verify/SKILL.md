---
name: plot-verify
description: Plot a lead-week verification grid from pre-computed verify Zarrs. Columns are observation, then week-1 through week-4 forecasts; the metric row sits under the forecasts. Every --obs and --forecast must already be a single time — run select on the verifying week first. Run verify on each forecast/obs pair before this skill. For precipitation, aggregate-temporal then convert-to-totals before verify. Pass --forecast week-1 first. Use --fontsize to enlarge labels (default 18).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py *)
metadata:
  version: "0.0.3"
  catalog-group: figure
---

# plot-verify

Lead-week **verification figure** for **one observation week**. This skill
**plots only** — it does not compute verification and it does not pick a
time. Run `select` so every cube is one verifying week, run `verify` on
each forecast/obs pair, then pass those Zarrs here.

**`--obs` and each `--forecast` must have a single time (size 1).** If you
see `has time size N; select the verifying week`, run `select` first
(`--dim time --value <week start>`). A leftover `step` axis needs
`step-to-time` first.

Columns are **observation, then week-1 through week-4** (most recent lead
on the left). Pass `--forecast` in that order (week-1 first).

| | Obs | 1-week lead | 2-week lead | 3-week lead | 4-week lead |
| --- | --- | --- | --- | --- |
| forecast row | obs map | week-1 | week-2 | week-3 | week-4 |
| metric row | (empty) | verify | verify | verify | verify |

The metric row comes from `--verify` Zarrs. All `--verify` inputs must
share the same `verify_metric` (`hits`, `bias`, or `mae`). Regional
scores are read from each verify Zarr's `verify_score_summary` attr.

## Pipeline (one obs week)

1. Prepare obs and each lead's forecast: aggregate, `step-to-time` if
   needed, **`select` the verifying week**, coarsen obs onto the forecast
   grid.
2. For each lead, run `verify`.
3. Pass single-time obs, then forecasts week-1 … week-4, with matching
   `--verify` Zarrs.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs <obs.zarr> \
    --forecast <week1.zarr> --verify <verify_w1.zarr> \
    --forecast <week2.zarr> --verify <verify_w2.zarr> \
    ... \
    -o <out.png> [--variable NAME] \
    [--lead "1-week lead" ...] [--title TEXT] [--fontsize N] [--colormap NAME] \
    [--bbox N/W/S/E] [--mask-geojson PATH]
```

### Arguments

- `--obs` — observation Zarr for the verifying week (required). Must
  already be one time.
- `--forecast` — forecast Zarr for that same week at one lead. Pass
  **week-1 first**, then week-2, week-3, week-4. Repeat with matching
  `--verify`. Must already be one time.
- `--verify` — verify Zarr from the `verify` skill for that lead.
  **Required once per `--forecast`**, same order.
- `--variable`, `-v` — obs/forecast data variable (verify Zarrs carry
  their own verification variable).
- `--lead` — column title, once per `--forecast`. Default: `1-week lead`
  … `N-week lead`.
- `--label` — pass once for `--obs`, then once per `--forecast`. The obs
  value titles the observation column; the verify row uses the metric
  name (Hits, Bias, MAE). When omitted, labels are inferred from provenance.
- `--fontsize` — base font size (default 18). Title is larger than column
  headers; lat/lon ticks stay smaller.
- `--colormap`, `--title`, `--bbox`, `--mask-geojson`, `--output` — as before.
  A long `--title` (or title plus verifying-week dates) wraps onto a second
  line.

### Output

A PNG with observation in column 0 and N lead columns of forecast +
verify maps. Stdout echoes each column's `verify_score_summary`. The
verify colorbar is metric-specific: hits use disagree / below / hit;
bias uses a diverging scale centered on zero; MAE uses white at zero
through warm colors. The verifying week dates are added to the figure
title when the obs time coordinate can be read.

## Example

```bash
for w in 1 2 3 4; do
  uv run skills/verify/scripts/verify.py \
    --forecast /tmp/s2s_week${w}.zarr --obs /tmp/chirps_week.zarr \
    --metric hits --threshold 1 -o /tmp/verify_w${w}.zarr
done

uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs /tmp/chirps_week.zarr \
    --forecast /tmp/s2s_week1.zarr --verify /tmp/verify_w1.zarr \
    --forecast /tmp/s2s_week2.zarr --verify /tmp/verify_w2.zarr \
    --forecast /tmp/s2s_week3.zarr --verify /tmp/verify_w3.zarr \
    --forecast /tmp/s2s_week4.zarr --verify /tmp/verify_w4.zarr \
    --variable precip --bbox 5/34/-5/42 \
    --title "Kenya weekly precip verification" \
    -o /tmp/verify_week.png
```

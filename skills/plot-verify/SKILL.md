---
name: plot-verify
description: Plot a lead-week verification grid from pre-computed verify Zarrs. Observation is shown once (the verifying week); columns are week-4 through week-1 forecasts with the verify metric under each. Every --obs and --forecast must already be a single time — run select on the verifying week first (a weekly GEFS cube still has ~5 times after aggregate-temporal). Run verify on each forecast/obs pair before this skill. For precipitation, aggregate-temporal then convert-to-totals before verify. Use --fontsize to enlarge column/row labels, ticks, and colorbars (default 18).
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

**`--obs` and each `--forecast` must have a single time (size 1).** A
weekly GEFS (or S2S) cube after `aggregate-temporal --period weekly` still
has several valid times (often 5: week-0 through week-4). `verify` may
inner-join down to one time; the forecast file does not. If you see
`has time size N; select the verifying week`, run `select` first:

```bash
uv run skills/select/scripts/select_dim.py \
    --dim time --value 2026-08-30 \
    --input /tmp/gefs_w4_weekly.zarr --output /tmp/gefs_w4_week.zarr
```

Use the same `--value` as the obs week (ISO date, exact match). Do that
for obs and for every lead before `verify` and before this skill. A
leftover `step` axis needs `step-to-time` first, then `select` on `time`.

Columns run **least recent to most recent** (4-week lead on the left,
1-week lead on the right). Observation is drawn **once**, titled with the
verifying week dates. Column titles name the forecast lead, not a
different obs week.

| | 4-week lead | 3-week lead | 2-week lead | 1-week lead |
| --- | --- | --- | --- | --- |
| obs product | one map (verifying week) | | | |
| forecast product | week-4 map | week-3 map | week-2 map | week-1 map |
| Verification | verify map | verify map | verify map | verify map |

The verification row comes from `--verify` Zarrs (output of the `verify`
skill). All `--verify` inputs must share the same `verify_metric`
(`hits`, `bias`, or `mae`). Regional scores are read from each verify
Zarr's `verify_score_summary` attr (stamped by `verify`).

## Pipeline (one obs week)

1. Prepare obs and each lead's forecast: aggregate, `step-to-time` if
   needed, **`select` the verifying week** (`--dim time --value <week
   start>`), coarsen obs onto the forecast grid. Every file passed to
   `verify` / `plot-verify` is then one time.
2. For each lead, run `verify`:

```bash
uv run skills/verify/scripts/verify.py \
    --forecast /tmp/s2s_week4.zarr --obs /tmp/chirps_week.zarr \
    --metric hits --threshold 1 -o /tmp/verify_w4.zarr
# repeat for week 3, 2, 1 …
```

3. Pass those single-time obs, forecasts, and verify Zarrs here (week-4 first).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs <obs.zarr> \
    --forecast <week4.zarr> --verify <verify_w4.zarr> \
    --forecast <week3.zarr> --verify <verify_w3.zarr> \
    ... \
    -o <out.png> [--variable NAME] \
    [--lead "4-week lead" ...] [--title TEXT] [--fontsize N] [--colormap NAME] \
    [--bbox N/W/S/E] [--mask-geojson PATH]
```

### Arguments

- `--obs` — observation Zarr for the verifying week (required). Must
  already be one time.
- `--forecast` — forecast Zarr for that **same** week at one lead. Repeat
  with matching `--verify`. Must already be one time; this skill will not
  choose among several valid times.
- `--verify` — verify Zarr from the `verify` skill for that lead.
  **Required once per `--forecast`**, same order.
- `--variable`, `-v` — obs/forecast data variable (verify Zarrs carry
  their own verification variable).
- `--lead` — column title, once per `--forecast`. Default: `N-week lead`
  … `1-week lead` so columns read as forecast lead time, not as different
  observation weeks.
- `--label` — row title override. Pass once for `--obs`, then once per
  `--forecast` (same order). The forecast row uses one label when all match,
  otherwise joins unique labels with ` / `. The verify row stays the metric
  name (Hits, Bias, MAE). When omitted, row titles are inferred from provenance.
- `--fontsize` — base font size for column/row labels, ticks, and colorbars
  (default 18). Raise on user request (e.g. `--fontsize 22`).
- `--colormap`, `--title`, `--bbox`, `--mask-geojson`, `--output` — as before.

### Output

A PNG with one observation map above an N-column forecast + verify grid.
Stdout echoes each column's `verify_score_summary` from the corresponding
`--verify` Zarr. The verify-row colorbar is metric-specific: hits use
disagree / below / hit classes; bias uses a brown (dry) ↔ white ↔ blue
(wet) scale centered on zero; MAE uses white at zero through warm colors.
The observation panel is titled with the verifying week dates. Colorbar
strips are short; tick and label type is large.

## Example

```bash
# Each cube is already one verifying week (select first if time size > 1)
for w in 4 3 2 1; do
  uv run skills/verify/scripts/verify.py \
    --forecast /tmp/s2s_week${w}.zarr --obs /tmp/chirps_week.zarr \
    --metric hits --threshold 1 -o /tmp/verify_w${w}.zarr
done

uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs /tmp/chirps_week.zarr \
    --forecast /tmp/s2s_week4.zarr --verify /tmp/verify_w4.zarr \
    --forecast /tmp/s2s_week3.zarr --verify /tmp/verify_w3.zarr \
    --forecast /tmp/s2s_week2.zarr --verify /tmp/verify_w2.zarr \
    --forecast /tmp/s2s_week1.zarr --verify /tmp/verify_w1.zarr \
    --variable precip --bbox 5/34/-5/42 \
    --title "Kenya weekly precip verification" \
    -o /tmp/verify_week.png
```

---
name: plot-verify
description: Plot a lead-week verification grid from pre-computed verify Zarrs. Columns are observation, then week-1 through week-4 forecasts; the metric row sits under the forecasts. Every --obs and --forecast must already be a single time — run select on the verifying week first. Run verify on each forecast/obs pair before this skill. For precipitation, aggregate-temporal then convert-to-totals before verify. Pass --forecast week-1 first. Set theme.fontsize in --spec to enlarge labels (default 16).
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

## Before guessing a flag or a key

Every drawing choice besides `--obs`/`--forecast`/`--verify`/`--output` is a
JSON key under `--spec` (see **Parameters** below); an unknown or misplaced
key is a hard error listing every valid key at that level, not a silent
no-op. If you don't already know the shape of `--spec`, run `--dump-spec -`
with your `--obs`/`--forecast`/`--verify` files and no `--spec` at all to
see the whole resolved schema in one call, rather than guessing keys one at
a time. And render once and look at the PNG (or run `inspect-figure` on it)
before trying another variation — the `plot hash` printed after a render
only tells you the pixels changed, never what changed or how it looks.

**`--obs` and each `--forecast` must have a single time (size 1).** If you
see `has time size N; select the verifying week`, run `select` first
(`--dim time --value <week start>`). A leftover `step` axis needs
`step-to-time` first.

Columns are **observation, then week-1 through week-4** (week-1 next to
obs). Pass `--forecast` week-1 first. Column titles are `traces[0].leads`.
If those titles include week numbers (e.g. `Week 4 (init …)`), columns are
sorted week-1 → week-4 even when you pass week-4 first. Label size is
`theme.fontsize` (default 16).

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
   grid. This figure plots those already-aligned fields. It does not draw
   a native-resolution observation next to a coarser forecast. `plot --layer`
   draws each dataset on its own grid.
2. For each lead, run `verify`.
3. Pass single-time obs, then forecasts week-1 … week-4, with matching
   `--verify` Zarrs.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs <obs.zarr> \
    --forecast <week1.zarr> --verify <verify_w1.zarr> \
    --forecast <week2.zarr> --verify <verify_w2.zarr> \
    -o <out.png> \
    --spec '{"inputs":[{"id":"obs","variable":"precip"}],"title":"Kenya weekly precip verification","geo":{"bbox":[5,34,-5,42]}}'
```

### Arguments

- `--obs` — observation Zarr for the verifying week. Must already be one time. Optional when `--spec` lists an obs input.
- `--forecast` — forecast Zarr for that same week at one lead. Pass **week-1 first**. Repeat with a matching `--verify`. Must already be one time.
- `--verify` — verify Zarr for that lead, once per `--forecast`, same order.
- `--output`, `-o` — PNG path.
- `--spec` — JSON object or path, always deep-merged onto the spec built from the opened files. Your values win. A `patch` key inside the object is rejected.
- `--dump-spec` — write the merged spec as JSON and skip the PNG. Bare `--dump-spec` or `-` prints to stdout. Run it with just the dataset flags and no `--spec` to see the full default schema before writing one.

### Parameters (`--spec`)

- `inputs[]` — `variable` per input (`obs`, `forecast1`, `forecast2`, …); an input that omits it uses `inputs[0].variable` (the obs input), then auto-detects. Obs and a forecast may name the field differently (e.g. `precip` vs `precipitation_surface`) — set each one's own `variable`. `label` on obs and each forecast.
- `title`, `theme.fontsize` (default 16), `theme.colormap`, `vmin`, `vmax`.
- `traces[0].leads` — column titles. Default `1-week lead` … `N-week lead`. Titles that name a week are sorted so week-1 sits next to the observation.
- `geo.bbox` as `[N, W, S, E]`, `geo.mask_geojson`.
- `layout.figsize` as `[W, H]`, `layout.facet.wspace` / `hspace`.

### Output

A PNG with observation in column 0 and N lead columns of forecast +
verify maps. Two colorbars sit **side by side at the bottom**: values
(obs/forecast) on the left, the verify metric (hits / bias / MAE) on
the right. Stdout also prints `plot hash` (sha256 of RGB pixels) and
`data: not null` or `data: NULL`, then each column's `verify_score_summary`.
A changed hash only proves the pixels differ, not what changed or whether
it looks right — never use hash comparisons to answer a layout or
appearance question; always look at the PNG. `NULL` means inspect-zarr the
inputs. Hits
use disagree / below / hit; bias uses a diverging scale centered on
zero; MAE uses white at zero through warm colors. The verifying week
dates are added to the figure title when the obs time coordinate can
be read.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs /tmp/chirps_week.zarr \
    --forecast /tmp/s2s_week1.zarr --verify /tmp/verify_w1.zarr \
    --forecast /tmp/s2s_week2.zarr --verify /tmp/verify_w2.zarr \
    --forecast /tmp/s2s_week3.zarr --verify /tmp/verify_w3.zarr \
    --forecast /tmp/s2s_week4.zarr --verify /tmp/verify_w4.zarr \
    -o /tmp/verify_week.png \
    --spec '{"inputs":[{"id":"obs","variable":"precip"}],"title":"Kenya weekly precip verification","geo":{"bbox":[5,34,-5,42]}}'
```

---
name: aggregate-temporal
description: Roll up a weather-skills standard dataset Zarr along its time axis (or forecast step axis) into windows (daily, weekly, dekadal = a fixed 10 days, calendar monthly, or a pint duration like '21 day') or a rolling --window, with mean/min/max. Stamps the new output spacing as data_interval for --period, plus aggregation_period, aggregation_coverage, and CF cell_methods. Rates in and rates out — use convert-to-totals for period amounts (refuses overlapping rolling series; select non-overlapping times first). Incomplete bins are kept, listed on stderr, and dropped later by convert-to-totals.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# aggregate-temporal

Source-agnostic temporal aggregation of **rates**. Works on:
- Observation datasets with a `time` dim (e.g. CHIRPS, IMERG, TAHMO).
- Forecast datasets with a `step` dim (e.g. ECMWF S2S).

Autodetects which dim is present. For forecasts, aggregates ensemble members (`number`) independently.

## When to use

- Turning daily or half-hourly rates into weekly/dekadal/monthly/custom-duration
  **mean** (or min/max) rates (`--period weekly` or `--period '21 day'`).
- Stamping `aggregation_period` on a cube that is **already** that period
  (weekly downscaled forecast, `data_interval` already `7 day`). Values and
  the time/step axis stay unchanged so `convert-to-totals` can run. Do not
  use this to "re-aggregate" already-weekly Kenya `precip_downscaled` —
  that product is stamped on fetch.
- Rolling N-step means (`--window`) with optional `--align` / `--stride`.
- Selecting weekly or dekadal subsets of a forecast initialized at multiple steps.
- For period **totals** (`mm`), run `convert-to-totals` afterward (non-overlapping bins only; rolling series with Δt &lt; `aggregation_period` are refused — `select` the times you want first). A single remaining bin is allowed. Incomplete bins are kept and stamped with `aggregation_coverage` &lt; 1; convert-to-totals `--min-coverage` (default 1.0) drops them.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --period daily|weekly|dekadal|monthly|'21 day' [--method mean|max|min] \
    [--variable VAR ...] [--time-dim DIM] \
    [--start-time YYYY-MM-DD] [--end-time YYYY-MM-DD]

uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --window N [--align left|right|center] [--stride STEP] \
    [--method mean|max|min] [--variable VAR ...] [--time-dim DIM]
```

Exactly one of `--period` or `--window` is required.

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--period` — calendar/step window: `daily` (1d), `weekly` (7d), `dekadal`
  (a fixed 10 days back to back — **not** calendar dekads 1–10 / 11–20 /
  21–end of month), `monthly` (calendar months, 1st to 1st, in the input's
  own calendar — `noleap` and `360_day` months included; refused with
  `--end-time` and on `step`), or a pint duration in whole days (`21 day`,
  `3 week`). Mutex with `--window`. Use a duration when you need a window the
  named periods do not cover (e.g. 21-day totals from half-hourly IMERG).
  **Calendar dekad:** fetch that dekad and pass its length with `--end-time`
  set to the day after it, e.g. 21–31 Oct is `--period '11 day' --end-time
  2026-11-01`; 21–28 Feb is `--period '8 day' --end-time 2026-03-01`.
- `--window` — rolling window length in axis steps. Mutex with `--period`.
  Refused when the input has CF `{dim}_bounds`, or missing rows (spacing
  other than `data_interval`): N rows across a gap span more than N
  intervals. Use `--period`, which counts missing rows against coverage.
- `--align` — with `--window`: label placement `left` (default), `right`, or `center`.
- `--stride` — with `--window`: integer subsample step, or a date stride (`day`, `week`, `month`, `year`, or weekday names like `Monday`).
- `--method` — reducer: `mean` (default), `max`, `min`. There is no `sum`; totals are a separate skill,
  and only a `mean` converts to a total (a weekly max × 7 days is not a weekly total).
- `--variable`, `-v` — repeatable; restricts aggregation to the named data variable(s).
- `--time-dim` — override; by default uses `time` if present, else `step`.
- `--end-time` — with `--period` on a `time` axis: exclusive right edge of
  the final bin (bins walk backward). Labeled at the **left** edge, same
  as forecast `step` buckets — a week with `--end-time 2026-08-31` is
  `[2026-08-24, 2026-08-31)` labeled `2026-08-24`. Fetch and resolve-time
  end dates are **inclusive**, so pass the day after: data fetched through
  2026-09-30 takes `--end-time 2026-10-01`. Samples at or after `--end-time`
  are left out and counted on stderr. No effect on `step` or `--window`.
- `--start-time` — with `--period` and `--end-time`: optional earliest coverage floor. Requires `--end-time`.

### Re-aggregating aggregated data

Input that is already aggregated (e.g. weekly) is binned by whole samples:
each bin must hold a whole number of input samples. `--period` must be a
multiple of the input spacing (weekly → `14 day` or `28 day`), and with
`--end-time` the edge must sit on the input's own week grid. Weekly →
`monthly`, `dekadal`, `10 day` or `daily` exits 2: a week straddles the bin
edge. Aggregate from the daily data instead. A partial input bin counts at
its own `aggregation_coverage`, so a 3/7 week inside a 14-day bin makes that
bin 10/14, not complete.

### Missing data is never filled

A cell or station missing samples in a bin that other cells have is **NaN**
for that bin, not a mean of the days it did report (a station with 2 of 7
days gets no weekly value; convert-to-totals would otherwise scale it to 7
days). Gaps shared by every cell are left to `aggregation_coverage`. Do not
work around this by hand (`skipna` sums, `fillna`, interpolation, "mean ×
days"); report incomplete stations as missing.

### Metadata stamped

On each aggregated data variable:

- `data_interval` — sample spacing of the **output**. A `--period` roll-up
  sets it to the period (daily → `--period '21 day'` gives `21 day`). A
  rolling `--window`, or an input already at the period, keeps the native
  spacing from the fetch.
- `aggregation_period` — pint duration for the window (`1 day`, `7 day`,
  `1 dekad`, `1 month`, `21 day`, or e.g. `7 day` for `--window 7` on daily
  data) used later by `convert-to-totals`. `1 month` is a calendar period:
  convert-to-totals multiplies each month-start label by that month's real
  length (February 28/29 days), and `select` stamps the real length
  (`28 day`) when it collapses the time axis.
- `cell_methods` — CF statistic, e.g. `time: mean (interval: 1 day)`.
  `interval:` is the **input** sample spacing (the native `data_interval`).

On the time/step axis:

- `aggregation_coverage` — completeness of each interval vs native cells
  (0–1). Incomplete bins are kept. A native sample counts only if it has
  **finite data** (all-NaN unpublished forecast leads do not). A persistent
  spatial hole (land mask) does not mark the time as missing. Uniform:
  finite count / (`aggregation_period / native spacing`); a missing row
  counts against its own bin. Irregular CF bounds: finite covered duration /
  window. convert-to-totals
  `--min-coverage` (default 1.0) drops incomplete bins. Every incomplete bin
  is listed on stderr as a warning. Interior incomplete bins usually mean the
  input has gaps (e.g. one week per year after `concat`): fixed-width bins
  run back to back from the first timestamp, so each segment straddles two
  bins. Aggregate each segment before `concat` instead.

Inputs with CF `{dim}_bounds` are duration-weighted (`sum(rate × dt) / sum(dt)`), so a 1-day cell and a 5-day cell in the same week are not equal-weighted. `--window` is a step count and is refused on those axes.

Forecast `step` bins and `--end-time` observation bins are the same
geometry: half-open `[left, right)` labeled at the **left** edge. Week 1
of a forecast initialized 24 Aug is `0d` / `2026-08-24`; the observation
week for `--end-time 2026-08-31` is also `2026-08-24`. Both cover
`[2026-08-24, 2026-08-31)`. Forward `--period` resample (no `--end-time`)
is already left-labeled.

Rate `units` are unchanged (still `mm day-1`, etc.).

### Output

Same variables; the time/step axis is replaced by the aggregated window.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

---
name: aggregate-temporal
description: Roll up a weather-skills standard dataset Zarr along its time axis (or forecast step axis) into fixed windows (daily, weekly, dekadal, monthly, or a pint duration like '21 day') or a rolling --window, with mean/min/max. Stamps the new output spacing as data_interval for --period, plus aggregation_period, aggregation_coverage, and CF cell_methods. Rates in and rates out — use convert-to-totals for period amounts (refuses overlapping rolling series; select non-overlapping times first).
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
- For period **totals** (`mm`), run `convert-to-totals` afterward (non-overlapping bins only; rolling series with Δt &lt; `aggregation_period` are refused — `select` the times you want first). A single remaining bin is allowed. Incomplete bins are kept and stamped with `aggregation_coverage` &lt; 1; convert-to-totals `--min-coverage` (default 1.0) drops them. Individual cells/stations with missing days are NaN (see *Missing data is never filled*).

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
- `--period` — calendar/step window: `daily` (1d), `weekly` (7d), `dekadal` (10d),
  `monthly` (calendar month for the default forward resample; 30-day fixed width
  with `--end-time`), or a pint duration in whole days (`21 day`, `3 week`).
  Mutex with `--window`. Use a duration when you need a window the named
  periods do not cover (e.g. 21-day totals from half-hourly IMERG).
- `--window` — rolling window length in axis steps. Mutex with `--period`.
  Refused when the input has CF `{dim}_bounds` (use `--period`).
- `--align` — with `--window`: label placement `left` (default), `right`, or `center`.
- `--stride` — with `--window`: integer subsample step, or a date stride (`day`, `week`, `month`, `year`, or weekday names like `Monday`).
- `--method` — reducer: `mean` (default), `max`, `min`. There is no `sum`; totals are a separate skill.
- `--variable`, `-v` — repeatable; restricts aggregation to the named data variable(s).
- `--time-dim` — override; by default uses `time` if present, else `step`.
- `--end-time` — with `--period` on a `time` axis: exclusive right edge of
  the final bin (bins walk backward). Labeled at the **left** edge, same
  as forecast `step` buckets — a week with `--end-time 2026-08-31` is
  `[2026-08-24, 2026-08-31)` labeled `2026-08-24`. Copy the `YYYY-MM-DD`
  from fetch / resolve-time. No effect on `step` or `--window`.
- `--start-time` — with `--period` and `--end-time`: optional earliest coverage floor. Requires `--end-time`.
- `--keep-partial-cells` — with `--period`: keep cells/stations that are missing
  samples other cells have (see below). Stamps `aggregation_partial_cells: kept`;
  `convert-to-totals` refuses such variables. Never use it for totals.

### Missing data is never filled

A cell or station that is missing samples in a bin **that other cells have**
becomes **NaN** for that bin. It does not get a mean of the days it did report.
Example: a TAHMO station that reported 2 of 7 days gets no weekly value. If
it got the mean of those 2 days, `convert-to-totals` would multiply it by
7 days and invent rain that no gauge measured. Stderr reports how many
cell-bins were masked. Gaps shared by every cell (an unpublished lead, a
trailing partial week) are left to `aggregation_coverage` and the
convert-to-totals `--min-coverage` gate. Cells that are NaN for the whole bin
(land mask) stay NaN. Rolling `--window` needs a full window per cell
(`min_periods = window`).

Do not work around this by hand: no `skipna` sums or means, `fillna`,
interpolation, or "mean × days" in ad-hoc code. Report stations or cells
with missing days as missing, and say how many were dropped.

### Metadata stamped

On each aggregated data variable:

- `data_interval` — sample spacing of the **output**. A `--period` roll-up
  sets it to the period (daily → `--period '21 day'` gives `21 day`). A
  rolling `--window`, or an input already at the period, keeps the native
  spacing from the fetch.
- `aggregation_period` — pint duration for the window (`1 day`, `7 day`,
  `1 dekad`, `1 month`, `21 day`, or e.g. `7 day` for `--window 7` on daily
  data) used later by `convert-to-totals`.
- `cell_methods` — CF statistic, e.g. `time: mean (interval: 1 day)`.
  `interval:` is the **input** sample spacing (the native `data_interval`).

On the time/step axis:

- `aggregation_coverage` — completeness of each interval vs native cells
  (0–1). Incomplete bins are kept. A native sample counts only if it has
  **finite data** (all-NaN unpublished forecast leads do not). A persistent
  spatial hole (land mask) does not mark the time as missing. Uniform:
  finite count / (`aggregation_period / native spacing`). Irregular CF
  bounds: finite covered duration / window. convert-to-totals
  `--min-coverage` (default 1.0) drops incomplete bins.

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

History entry records period/window, method, variables, and paths as usual.

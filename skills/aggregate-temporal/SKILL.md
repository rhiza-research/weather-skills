---
name: aggregate-temporal
description: "Roll up a weather-skills Zarr along its time axis or forecast lead (step) axis into fixed windows (sub-daily such as 6 hours, daily, weekly, 10-day, calendar monthly, or any whole number of days) or a rolling window, taking the mean, min, or max. Use to turn hourly rates into 6-hour means, or daily rates into weekly, dekadal, or monthly mean rates; each output window is stamped with its length and how complete it is."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py *)
metadata:
  version: "0.0.5"
  catalog-group: transforms
---

# aggregate-temporal

Averages (or takes the min or max of) a dataset's samples over time windows; rates in, rates out.

## Usage

```bash
# Fixed windows
uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py -i IN.zarr -o OUT.zarr --period PERIOD [--end-time YYYY-MM-DD [--start-time YYYY-MM-DD]] [--method mean|max|min] [-v VAR ...] [--time-dim DIM]
# Rolling window
uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py -i IN.zarr -o OUT.zarr --window N [--align left|right|center] [--stride S] [--method mean|max|min] [-v VAR ...] [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — input Zarr with a `time` or `step` axis.
- `--output`, `-o` — output Zarr.
- `--period` — `daily`, `weekly` (7 days), `dekadal` (a fixed 10 days back to back, not calendar dekads), `monthly` (calendar months, `time` only, in the input's own calendar), or a duration such as `21 day`, `3 week`, or sub-daily `6 hour` (bins start at 00 UTC on `time` and at lead 0 on `step`). Pass exactly one of `--period` and `--window`.
- `--window` — rolling window length in axis rows.
- `--align` — with `--window`: label each window at its `left` (first) row, `right` (last) row, or `center`. Default: `left`.
- `--stride` — with `--window`: keep every Nth window (integer), or keep dates by `day`, `week`, `month`, `year`, or weekday names (`Monday`, `Monday/Thursday`). Date strides need every picked date on the axis (daily data); on weekly data use an integer.
- `--method` — `mean`, `max`, or `min`. Default: `mean`.
- `--variable`, `-v` — aggregate only this variable; repeat for several. Default: all.
- `--time-dim` — axis to aggregate. Default: `time`, or `step` when there is no `time` axis or `time` has one value.
- `--end-time` — with `--period` on `time`: exclusive right edge of the last window; windows walk backward from it. Pass the day after the last day you want (data through 2026-10-14 takes `--end-time 2026-10-15`). Later samples are dropped and counted on stderr. Not allowed with `monthly`; ignored on `step`.
- `--start-time` — with `--end-time`: stop creating windows before this date.

### Output
- Same variables and units; the aggregated axis holds one label per window, at the window's left edge. Without `--end-time`, `time` windows start at the first sample; `step` windows start at lead 0. Ensemble members stay separate.
- Each variable gets `aggregation_period` (window length, e.g. `7 day`, `1 dekad`, `1 month`), `data_interval` (output spacing: the period, or the input spacing for `--window`), and CF `cell_methods` (e.g. `time: mean (interval: 1 day)`, where the interval is the input spacing).
- Coordinate `aggregation_coverage` (0–1) on the axis: the share of expected input samples with data. Incomplete windows are kept and listed on stderr.

### Notes
- A cell missing samples that other cells have is NaN for that window; values are never averaged over a partial record.
- `--window` is refused on axes with missing rows or CF `{dim}_bounds`; use `--period`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py -i chirps_kenya.zarr -o chirps_kenya_weekly.zarr \
  --period weekly --end-time 2026-10-15
```
Writes weekly mean CHIRPS rates (`mm day-1`) whose last week is 2026-10-08 to 2026-10-14, labeled 2026-10-08.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py -i gfs_precip.zarr -o gfs_precip_6h.zarr --period "6 hour" --time-dim step
```
Writes 6-hour mean GFS precip rates; hourly and 3-hourly leads are weighted by their `step_bounds`. Step 42 h is the 42–48 h mean.

## Details

- Input that is already aggregated must divide evenly into the period (weekly → `14 day`, not `monthly`), else exit 2. A period shorter than the input's samples (`6 hour` on daily data, or shorter than the widest `step_bounds` cell) also exits 2. Input already at the period is only stamped, not changed.
- Concatenated segments with gaps (e.g. one week per year) straddle windows; aggregate each segment before concatenating.

**Window geometry.** Windows are half-open `[left, right)` and labeled at the left edge, on `step` and with `--end-time` alike: week 1 of a forecast initialized 2026-10-12 is `step = 0` and covers the same days as the `--end-time 2026-10-19` observation week labeled 2026-10-12.

**Coverage.** A sample counts only when some cell has data (a fixed land mask does not count as missing; an all-NaN unpublished lead does). Re-aggregated input counts at its own coverage, so a 3/7 week inside a 14-day window gives 10/14.

**Irregular axes.** Inputs with CF `{dim}_bounds` are weighted by sample duration (`sum(rate × dt) / sum(dt)`).

**Calendar dekads.** Fetch the dekad and pass its length with `--end-time` the day after it: 21–31 Oct is `--period '11 day' --end-time 2026-11-01`.

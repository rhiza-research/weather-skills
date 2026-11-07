---
name: climatology
description: "Compute a daily climatology (mean, standard deviation, optional quantiles and sample count for each calendar day across years) from a multi-year daily weather-skills Zarr, optionally pooling neighbouring days or ensemble members, and lay it onto a real calendar date range in the same layout as clim-fetch. Use when a task needs a climatological baseline with a known base period, tercile or quintile boundaries, a model climatology from a hindcast archive, or for a dataset, variable or region that clim-fetch does not mirror."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/climatology.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# climatology

Groups a daily record by month and day, averages each calendar day over every year in the input, and writes the mean, the standard deviation, any requested quantiles and the number of values used. The base period is exactly the input's date range.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/climatology.py -i DAILY.zarr -o CLIM.zarr --start-time YYYY-MM-DD --end-time YYYY-MM-DD [-v VAR ...] [--window DAYS] [--quantiles Q,Q,...] [--pool-members] [--min-fraction F]
```

### Arguments
- `--input`, `-i` — Zarr of daily values covering at least two years: gridded, station, or a forecast archive with extra dims such as `step` or `number`. Dates may be sparse (weekly forecast inits) but never less than a day apart. Its date range is the base period.
- `--output`, `-o` — output Zarr, replaced if it exists.
- `--start-time`, `--end-time` — inclusive calendar window to lay the climatology onto; one output row per day.
- `--variable`, `-v` — variable to process; repeat for several. Default: every data variable along time.
- `--window` — odd number of calendar days pooled around each day (default `1`). `--window 5` uses each year's values from two days either side too, wrapping across the year end. Use it for short records or sparse inputs.
- `--quantiles` — comma-separated quantiles between 0 and 1 (e.g. `0.333,0.667` for tercile boundaries), taken over the same pooled values as the mean.
- `--pool-members` — treat the ensemble members (`number`) as extra samples, so all members share one climatology and `number` leaves the output. Without it each member gets its own.
- `--min-fraction` — leave a cell NaN in every statistic when fewer than this fraction of its possible values (dates in the window × members) exist (default `0`, keep any cell with a value).

### Output
- Dims as the input, with `time` holding one row per day of `--start-time`..`--end-time`.
- For each variable `V`: `V_avg` (mean), `V_std` (sample standard deviation, `ddof=1`), `V_qNN` per quantile (percent, `.` as `p`: `0.05` → `V_q05`, `0.333` → `V_q33p3`, with a `quantile` attr), all in the input units, and the coordinate `V_count` (number of values used, on the same dims; never masked). Same names as clim-fetch, so the output works with `standardize-anomaly` and `difference`.
- Global attrs `climatology_start_time` and `climatology_end_time` (first and last input dates), `climatology_window_days`, `climatology_pooled_members` (0 or 1) and `climatology_min_fraction`.

### Notes
- Values must be daily: sub-daily dates, or a variable without a `step` dim whose `aggregation_period` is not `1 day`, are refused. Roll them to daily with `aggregate-temporal` first (`--period daily`, which also works on a forecast's `step`), and clip or coarsen to the region before fetching many years: the whole input is read into memory (about 1.4 GB per 10,000 grid cells for 30 years of float32 days).
- Missing values are skipped, never filled; `V_count` shows how many values each day rests on. Days with no value anywhere stay NaN and stderr lists them.
- Feb 29 uses leap years only, so without `--window` it rests on about a quarter of the years. Every year in the output window gets the same values for the same month and day.
- For a forecast archive, days are grouped by the `time` coordinate (the init date) and each lead in `step` and member in `number` gets its own climatology. Build the archive by fetching each init, rolling `step` to daily, and stacking with `concat --dim time`. Weekly inits leave each calendar day about one value a year, so pass `--window` (e.g. 7 or 15); stderr notes sparse dates.
- On a forecast archive `aggregation_period` describes each lead window in `step`, so multi-day leads are accepted: roll `step` into weekly or longer windows with `aggregate-temporal --time-dim step` (and `convert-to-totals --time-dim step` for amounts), and the climatology and its quantiles are of those totals. Quantiles of totals must come from the totals; they cannot be built from daily quantiles.
- `--window` wraps across the year end, so Jan 1 also pools late-December dates from the year before.

### Example
After `chirps-fetch --start-time 1991-01-01 --end-time 2020-12-31 --bbox -6.9/110.9/-8.8/114.6 -o chirps_east_java_1991_2020.zarr`:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/climatology.py -i chirps_east_java_1991_2020.zarr -o chirps_east_java_clim.zarr --start-time 2026-01-01 --end-time 2026-12-31 --window 5
```
Writes the 1991–2020 CHIRPS daily rainfall climatology over East Java, pooled over 5 days, laid onto 2026 dates.

For a model climatology from a weekly-init hindcast, with members pooled and tercile boundaries:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/climatology.py -i hindcast_kenya.zarr -o hindcast_kenya_clim.zarr --start-time 2026-01-01 --end-time 2026-12-31 --window 21 --pool-members --quantiles 0.333,0.667 --min-fraction 0.5
```
Each calendar init day and lead pools the inits within ±10 days of it, over every year and member, and cells with under half the possible values stay NaN.

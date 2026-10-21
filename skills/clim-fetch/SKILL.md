---
name: clim-fetch
description: "Fetch a precomputed daily climatology (mean and standard deviation for each day of the year) of precipitation, sea-surface temperature, 2 m temperature or 10 m wind from a public Sheerwater mirror of IMERG, ERA5, CHIRPS, ECMWF IFS, GEFS or OISST, laid onto a real calendar date range, as a weather-skills Zarr. Use when a task needs a climatological baseline for anomalies, verification or comparison."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a static climatology Zarr from the public GCS bucket sheerwater-public-datalake over anonymous HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - precip
    - sst
---

# clim-fetch

Reads a day-of-year climatology and gives each date in the requested window the values for its month and day.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ID --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o OUT.zarr \
  [-v VAR] [--prediction-timedelta DAYS] [--window DAYS] [--align left|right|center] \
  [--approximate-spread none|upper-bound] [--bbox N/W/S/E]
```

| `--dataset` | `--variable` | Grid | Leads (days) |
| --- | --- | --- | --- |
| `imerg_final` | `precip` | 0.1° | 0 |
| `era5` | `precip` | 0.25° | 0 |
| `chirps` | `precip` | 0.05° | 0 |
| `ecmwf_ifs` | `precip`, `sst`, `uwind10m`, `vwind10m` | 1.5° | 0–45 |
| `gefs` | `precip`, `tmp2m` (2 m temperature) | 1° | 0–34 |
| `oisst` | `sst` | 0.25° | 0 |

An unmirrored pair (e.g. `era5` `tmp2m`) exits 1 and lists the available pairs.

### Arguments
- `--dataset` — source id from the table.
- `--start-time`, `--end-time` — inclusive calendar window; one output row per day.
- `--variable`, `-v` — variable from the table. Default: `precip`.
- `--prediction-timedelta` — forecast lead in whole days; the climatology of the value valid that many days after each init date. Default: 0. An unavailable lead exits and lists the available ones.
- `--window` — average over this many days (1 = daily). Default: 1. Above 1, requires a matching pre-aggregated store unless an approximate spread is explicitly requested.
- `--approximate-spread` — `none` (default) refuses when a multi-day store cannot be opened. `upper-bound` permits a daily-store roll-up and emits `mean(daily std)`, the perfect-correlation upper bound on window spread. Ignored for daily or pre-aggregated stores.
- `--align` — where each `--window` average is labelled: `left` (the window starts on the date), `right`, or `center`. Default: `left`. No effect with `--window 1`.
- `--bbox` — `N/W/S/E` decimal degrees. Omit for the full grid.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(latitude, longitude, time)`, one row per calendar day in the window.
- Variables `<var>_avg` (mean) and `<var>_std` (standard deviation), e.g. `precip_avg`, `precip_std`. Precip is `mm day-1`; `sst` and `tmp2m` are `degree_Celsius`; winds are `m/s`.
- Each variable has `aggregation_period` `"<window> day"`; `data_interval` is `1 day`. Global attrs `climatology_dataset`, `climatology_variable`, `climatology_prediction_timedelta_days`, `climatology_window_days`.
- The std records `spread_method` (`source_daily`, `source_pre_aggregated_window`, or `upper_bound_perfect_correlation`), `spread_is_approximate`, `spread_window_days`, and `spread_estimator`. Global attrs `climatology_spread_method` and `climatology_spread_is_approximate` repeat the method and approximation flag.

### Notes
- Always pass `--bbox` for `chirps` and `imerg_final`; without it the whole global year is read (tens of GB).
- Feb 29 is used only in leap years. Every year in the window gets the same values for the same month and day.
- For `ecmwf_ifs` and `gefs` at lead N, Jan 1 to Jan N are NaN; stderr lists every date with no value.
- A rolled upper bound stays zero for zero daily spread, and becomes NaN if any daily spread in the window is negative, missing, or infinite.
- Observation fetchers name the variable `precip` or `sst`, not `precip_avg`; rename the mean first (edit-coords `--rename precip_avg=precip`) before subtracting by name.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset chirps --start-time 2026-10-01 --end-time 2026-12-31 \
  --bbox 5.5/33.5/-5/42.5 -o chirps_clim_kenya.zarr
```
Writes daily CHIRPS `precip_avg` / `precip_std` over Kenya for October–December 2026.

## Details

**Source.** Each climatology is `gs://sheerwater-public-datalake/climatologies/<dataset>_<variable>_<window>d.zarr`, indexed by init dates in the leap year 1904 so every day of the year 1–366 has a row. For `--window` > 1 the skill first tries a matching pre-averaged store and otherwise refuses by default. With `--approximate-spread upper-bound`, it rolls the daily store, wrapping Dec 31 onto Jan 1.

**Window spread.** Daily means and standard deviations do not determine how days co-vary. The old independent-days shortcut `sqrt(mean(std²) / window)` is not an exact window spread or a universal lower bound. For perfectly correlated days of equal spread it understates the std by `sqrt(window)`, inflating standardized anomalies by the same factor. The explicit approximation instead uses `mean(daily std)`, an upper bound by the triangle inequality for standard deviations, and records that limitation in the output. For an empirical window spread, aggregate each base year's observations into the window and then take the standard deviation across years, recording the ddof. Daily and pre-aggregated source spreads pass through unchanged.

**Weekly totals.** `--window 7` gives the average daily rate over the 7 days starting on each date, still one row per day. For a climatology of weekly totals in mm, keep one row per week (every 7th date), then multiply each rate by 7 days. Both `_avg` and `_std` scale the same way.

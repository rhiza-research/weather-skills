---
name: clim-fetch
description: "Fetch a precomputed daily climatology (mean and standard deviation for each day of the year) of precipitation, sea-surface temperature, 2 m temperature or 10 m wind from a public Sheerwater mirror of IMERG, ERA5, CHIRPS, ECMWF IFS, GEFS or OISST, laid onto a real calendar date range, as a weather-skills Zarr. Use when a task needs a climatological baseline for anomalies, verification or comparison."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a static climatology Zarr from the public GCS bucket sheerwater-public-datalake over anonymous HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.3"
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
  [-v VAR] [--prediction-timedelta DAYS] [--window DAYS] [--align left|right|center] [--bbox N/W/S/E]
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
- `--window` — average over this many days (1 = daily). Default: 1.
- `--align` — where each `--window` average is labelled: `left` (the window starts on the date), `right`, or `center`. Default: `left`. No effect with `--window 1`.
- `--bbox` — `N/W/S/E` decimal degrees. Omit for the full grid.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(latitude, longitude, time)`, one row per calendar day in the window.
- Variables `<var>_avg` (mean) and `<var>_std` (standard deviation), e.g. `precip_avg`, `precip_std`. Precip is `mm day-1`; `sst` and `tmp2m` are `degree_Celsius`; winds are `m/s`.
- Each variable has `aggregation_period` `"<window> day"`; `data_interval` is `1 day`. Global attrs `climatology_dataset`, `climatology_variable`, `climatology_prediction_timedelta_days`, `climatology_window_days`.
- `climatology_start_time` and `climatology_end_time` give the first and last dates the mirror averaged over (e.g. `1998-01-01` and `2024-12-31`). They are present only when the mirror records them; when it doesn't, stderr says so and the base period is unknown.

### Notes
- Always pass `--bbox` for `chirps` and `imerg_final`; without it the whole global year is read (tens of GB).
- Feb 29 is used only in leap years. Every year in the window gets the same values for the same month and day.
- For `ecmwf_ifs` and `gefs` at lead N, Jan 1 to Jan N are NaN; stderr lists every date with no value.
- With `--window` > 1, `_std` is `sqrt(mean(std²) / window)`, which assumes independent days and so is a lower bound; stderr warns.
- Observation fetchers name the variable `precip` or `sst`, not `precip_avg`; rename the mean first (edit-coords `--rename precip_avg=precip`) before subtracting by name.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset chirps --start-time 2026-10-01 --end-time 2026-12-31 \
  --bbox 5.5/33.5/-5/42.5 -o chirps_clim_kenya.zarr
```
Writes daily CHIRPS `precip_avg` / `precip_std` over Kenya for October–December 2026.

## Details

**Source.** Each climatology is `gs://sheerwater-public-datalake/climatologies/<dataset>_<variable>_<window>d.zarr`, indexed by init dates in the leap year 1904 so every day of the year 1–366 has a row. For `--window` > 1 the skill first tries a matching pre-averaged store and otherwise averages the daily one itself, wrapping Dec 31 onto Jan 1 so windows near the year end are full.

**Weekly totals.** `--window 7` gives the average daily rate over the 7 days starting on each date, still one row per day. For a climatology of weekly totals in mm, keep one row per week (every 7th date), then multiply each rate by 7 days. Both `_avg` and `_std` scale the same way.

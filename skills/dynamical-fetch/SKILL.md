---
name: dynamical-fetch
description: "Fetch a forecast or analysis from the dynamical.org catalog (GFS, GEFS, ECMWF IFS-ENS and AIFS, ICON-EU, MRMS, IMERG, CHIRPS) for an init date or date range over a region, as a weather-skills Zarr. No credentials needed. Also fetches GEFS hindcasts (1998-2019) for a window of init dates from Sheerwater's stores. Use it first for any of these products."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public Zarr from the dynamical.org open catalog (AWS Open Data) over HTTPS via the dynamical-catalog library; no credentials required. With --hindcast, reads Sheerwater's GEFS reforecast stores via gcsfs: precip from the public gs://sheerwater-public-datalake anonymously; temperature_2m from the private gs://sheerwater-datalake, which needs Google Cloud credentials (NEURAL_GCM_SERVICE_CREDENTIALS, GOOGLE_APPLICATION_CREDENTIALS, or Application Default Credentials).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - precipitation_surface
    - temperature_2m
    - temperature_850hpa
    - geopotential_height_500hpa
---

# dynamical-fetch

Fetches one dataset from the [dynamical.org catalog](https://dynamical.org/catalog/), subset by bbox, time, and variables, as a weather-skills standard dataset Zarr.

## Usage

```bash
# Forecast: one 00 UTC init
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ID --date YYYY-MM-DD [--bbox N/W/S/E] [--max-lead 240h] -v VAR [-v VAR ...] -o OUT.zarr
# Analysis: inclusive date range
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ID --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -v VAR -o OUT.zarr
# GEFS hindcasts: every init in an inclusive window
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gefs-forecast-35-day --hindcast --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [-v precip] [-v t2m] -o OUT.zarr
# Latest init or analysis day (no download); add --hindcast for the latest GEFS hindcast init
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest ID [--hindcast]
```

**Forecast ids** (use `--date`): `noaa-gefs-forecast-35-day` (31 members), `ecmwf-ifs-ens-forecast-15-day-0-25-degree` (51), `ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree` (101), `ecmwf-ifs-ens-forecast-46-day-6-hourly-1-5-degree` (101), `ecmwf-aifs-ens-forecast` (51), `noaa-gfs-forecast`, `ecmwf-aifs-single-forecast`, `dwd-icon-eu-forecast-5-day` (Europe).
**Analysis ids** (use `--start-time`/`--end-time`): `noaa-gfs-analysis`, `noaa-gefs-analysis`, `noaa-mrms-conus-analysis-hourly` (CONUS), `nasa-imerg-analysis-early`, `nasa-imerg-analysis-late`, `ucsb-chc-chirps-analysis-final`, `ucsb-chc-chirps-analysis-preliminary` (land, 60°S–60°N).
HRRR ids are rejected (projected grid).

### Arguments
- `--dataset` — catalog id from the lists above.
- `--date` — forecast init date; selects the 00 UTC init.
- `--max-lead` — forecasts only: keep leads up to this one (`240h`, `10D`); only those leads are downloaded. `0h` keeps only the initial state of instant fields (MSLP, t2m, winds); averaged and accumulated fields such as precip have no lead 0, so they are dropped with a warning (exit 2 if nothing else was requested). Default: every lead (GFS has 209).
- `--start-time`, `--end-time` — analysis range; `--end-time` includes the whole end day (half-hourly IMERG for one day is 48 steps). With `--hindcast`, the inclusive window of init dates.
- `--hindcast` — `noaa-gefs-forecast-35-day` only: fetch GEFS hindcasts instead of a forecast. Takes `--start-time`/`--end-time`; `--date` and `--max-lead` exit 2, as does any other `--dataset`. Variables: `precipitation_surface` (`-v precip`, the default) and `temperature_2m` (`-v t2m`); others exit 2 with the list.
- `--bbox` — `N/W/S/E`, longitudes −180..180; W > E crosses the antimeridian. Omit for the full grid.
- `--variable`, `-v` — catalog variable name; repeat for several. Aliases: `precip`/`tp`/`pr` → `precipitation_surface`; `sst` → `sea_surface_temperature`; `t2m` → `temperature_2m` (or 46-day `average_temperature_2m`); `msl` → `pressure_reduced_to_mean_sea_level`; `sp` → `pressure_surface`; `u10`/`v10`/`u100`/`v100` → `wind_u_10m`/`wind_v_10m`/`wind_u_100m`/`wind_v_100m`; `t`/`gh`/`u`/`v`/`w`/`q` → every pressure level of that field. An unknown name exits and lists `Available:`. Omitting `-v` fetches every field.
- `--probe-latest [ID]` — print the latest init or analysis date and exit. stderr notes a forecast init whose last lead is not published yet; a sub-daily analysis's latest day is partial (stderr says so).
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Forecasts: `(step, [number,] latitude, longitude)` with scalar `time` = init. Analyses: `(time, latitude, longitude)`. Pressure fields add `vertical` (hPa).
- GEFS hindcasts: `(time, number, step, latitude, longitude)`, `time` = each init, 11 members (`number` 0-10), 35 daily steps; `step = 0` is `[init, init + 1 day)`. 1° grid, `data_interval` `1 day`, `weather_skills_source` `sheerwater:...`. The forecast is 3- then 6-hourly at 0.25°, so aggregate it to days and coarsen it before comparing.
- Precip in `mm day-1`, air temperature in `degree_Celsius`, other units as published. Longitude is `[-180, 180)`.
- Requested windows or leads past what is published are written as available, with a `WARNING` on stderr; nothing is padded. No data at all exits 1.

### GEFS hindcasts
- They come from Sheerwater's GEFSv12 reforecast stores, not dynamical.org: Wednesday inits 1998-12-30 to 2019-08-28.
- `precipitation_surface` is public and needs no credentials. `temperature_2m` reads a private bucket: without Google Cloud access it exits 1 and says which credential to set.
- The two stores skip different weeks. A variable is NaN on inits its store lacks, never filled; stderr lists them.
- `temperature_2m` is one value per day as the source publishes it; the source does not say whether it is a daily mean.
- Both stores are tiled in lat/lon (precip in 6° tiles), so a small `--bbox` reads little.
- Without `--bbox` the whole 1° globe is held in memory before writing, about 100 MB per init per variable (about 5 GB a year): pass `--bbox` for long windows.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree \
  --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v precip -o ecmwf_s2s_kenya.zarr
```
Writes 101-member daily precip rates for 46 days over Kenya.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset noaa-gefs-forecast-35-day --hindcast \
  --start-time 2019-07-01 --end-time 2019-08-31 --bbox 5.5/33.5/-5/42.5 -v precip -o gefs_hindcast_kenya.zarr
```
Writes 11-member daily precip rates for every July-August 2019 GEFS hindcast init over Kenya.

## Details

**Forecast `step` is the interval start.** The catalog labels averaged fields (precip, 46-day daily means) at the end of their interval. When every selected field is NaN at lead 0, fetch drops that lead and shifts each step back one, so `step = 0` is the first interval and weekly bins line up with `aggregate-temporal`. Where lead spacing changes (3- to 6-hourly), fetch writes `step_bounds`. Instant fields (winds, pressure levels, GEFS `temperature_2m`) keep the catalog labels, so fetch averaged and instant fields separately when you need interval-start steps.

**Pressure levels.** Medium-range IFS/AIFS publish selected `*_Nhpa` fields (925/850/500 hPa); the 46-day product has a 10-level stack in `group=pressure_level`, opened automatically. GFS and ICON-EU publish none. `-v precip` does not pull IMERG's `precipitation_quality_index_surface`; ask for it by name.

More examples:

```bash
# IMERG Late, 30 days
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset nasa-imerg-analysis-late \
  --start-time 2026-09-14 --end-time 2026-10-13 --bbox 5.5/33.5/-5/42.5 -v precip -o imerg_kenya.zarr
# IFS-ENS 850/925 hPa temperature, stacked on vertical
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf-ifs-ens-forecast-15-day-0-25-degree \
  --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v t -o ifs_t_kenya.zarr
```

**GEFS hindcast sources.** `precipitation_surface` reads `gs://sheerwater-public-datalake/hindcasts/gefs_precip.zarr`, a public copy of Sheerwater's GEFS reforecast cache in 6° tiles. `temperature_2m` reads the cache itself, `gs://sheerwater-datalake/caches/gefs_chc_raw/global1_0_tmp2m.zarr` (Zarr v3, init times in days since 1960-01-01).

Per-dataset native dims and the coordinate mapping: [references/REFERENCE.md](references/REFERENCE.md).

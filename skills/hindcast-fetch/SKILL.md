---
name: hindcast-fetch
description: "Fetch ECMWF or GEFS subseasonal hindcasts (reforecasts) for a range of init dates over a region, as a multi-init weather-skills forecast Zarr: daily precipitation and 2 m temperature out to 46 (ECMWF) or 35 (GEFS) days, from the stores Sheerwater uses. Use when a task needs past forecasts for calibration, bias correction, skill scores or training against observations."
license: MIT
compatibility: "Requires Python 3.12 and uv. ECMWF reads the public WeatherBench2 reforecast in gs://weatherbench2 anonymously. GEFS reads the private gs://sheerwater-datalake/caches/gefs_chc_raw via gcsfs and needs Google Cloud credentials: NEURAL_GCM_SERVICE_CREDENTIALS (the raw service-account key JSON), GOOGLE_APPLICATION_CREDENTIALS (a service-account JSON file path), or Application Default Credentials from `gcloud auth application-default login`."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - tmp2m
    - tmax2m
    - tmin2m
  openclaw:
    envVars:
      - name: NEURAL_GCM_SERVICE_CREDENTIALS
        description: Full contents of a GCP service-account key JSON with read access to gs://sheerwater-datalake/caches/gefs_chc_raw. Needed for --dataset gefs only.
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON file that can read gs://sheerwater-datalake/caches/gefs_chc_raw. Used only if NEURAL_GCM_SERVICE_CREDENTIALS is unset.
---

# hindcast-fetch

Fetches every hindcast init in a date window and stacks them on one `time` axis.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf|gefs --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o OUT.zarr \
  [-v VAR ...] [--bbox N/W/S/E] [--issue-year YYYY] [--members]
# Latest hindcast init date (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [ecmwf|gefs]
```

| `--dataset` | Inits | Members | Leads (days) | Grid | `-v` | Credentials |
| --- | --- | --- | --- | --- | --- | --- |
| `ecmwf` | 1996–2021, the calendar dates of the Mon/Thu issues of one year | ensemble mean, or 10 perturbed with `--members` | 0–45 | 1.5° | `precip`, `tmp2m`, `tmax2m`, `tmin2m` | none |
| `gefs` | Wednesdays 1998-12-30 to 2019-08-28 | 11 (`number` 0–10) | 0–34 | 1° | `precip`, `tmp2m` | Google Cloud |

### Arguments
- `--dataset` — `ecmwf` or `gefs`.
- `--start-time`, `--end-time` — inclusive window of hindcast init dates (the dates the runs start from, not when they were issued).
- `--variable`, `-v` — repeatable. Default: `precip`.
- `--bbox` — `N/W/S/E` decimal degrees. Omit for the full global grid.
- `--issue-year` — `ecmwf` only: the year (2016–2022) whose model cycle ran the hindcasts. Each issue year carries the 20 years before it. Default: the latest issue year that covers the whole window.
- `--members` — `ecmwf` only: fetch the 10 perturbed members instead of the ensemble mean. Reads about 10 times more.
- `--probe-latest` — print the latest hindcast init date (`YYYY-MM-DD`) and exit. Optional value `ecmwf` (default) or `gefs`.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, [number,] step, latitude, longitude)`: `time` is each hindcast's init date, `step` is daily and labels the start of each 24 h window, so `step=0` is `[init, init+1d)`. `ecmwf` without `--members` has no `number` dim.
- `precip` in `mm day-1`; `tmp2m`, `tmax2m`, `tmin2m` in `degree_Celsius`. Longitude is `[-180, 180)`.
- `ecmwf` adds an `issue_time` coordinate on `time`: the real-time forecast date the hindcast was run alongside.
- `data_interval` `1 day`. Global attrs `weather_skills_source`, `hindcast_dataset`, and for `ecmwf` `hindcast_issue_year`.

### Notes
- One `ecmwf` call uses one issue year, so a window may span at most 20 years; a window no issue year covers exits 2 with the coverage.
- `ecmwf` hindcast inits fall on the same calendar dates in every year, but on any weekday. Pair them with `gefs` Wednesday inits by valid date, not by init.
- `ecmwf` `precip` at lead day 15 can be negative (down to about −50 mm day-1) where the source's accumulations are split. Values are kept as published; stderr names the lead days. Average over a week or more, or drop that step, before using daily values.
- The `gefs` `precip` and `tmp2m` stores skip different weeks. A variable is NaN on inits its store lacks; stderr lists them.
- `ecmwf` is stored as whole globes: each init costs about 5 MB per variable (50 MB with `--members`) whatever the `--bbox`, so a 7-year window takes several minutes. `gefs` is stored in 24° tiles, so a small `--bbox` reads little.
- Without GEFS access the skill exits 1 and says which credential to set.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf --start-time 2011-10-01 --end-time 2020-12-31 \
  --bbox 5.5/33.5/-5/42.5 -o ecmwf_hindcast_kenya.zarr
```
Writes ECMWF ensemble-mean daily precipitation hindcasts over Kenya for every 2022-cycle init from October 2011 to December 2020.

## Details

**Sources.** `ecmwf` reads `gs://weatherbench2/datasets/ifs_extended_range/daily/ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr` (or `…-avg.zarr` for members), the WeatherBench2 copy of the ECMWF IFS extended-range reforecast that Sheerwater ingests. It is indexed by issue date (2016-01-04 to 2022-12-29) and hindcast year (−20 to −1); the skill picks the pairs whose real init falls in the window. Precipitation there is a 24 h total in metres. `gefs` reads `gs://sheerwater-datalake/caches/gefs_chc_raw/global1_0_<variable>.zarr`, Sheerwater's GEFS reforecast cache (Zarr v3, init times in days since 1960-01-01).

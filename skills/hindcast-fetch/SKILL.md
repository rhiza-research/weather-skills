---
name: hindcast-fetch
description: "Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts (reforecasts) for a range of init dates over a region, as a multi-init weather-skills forecast Zarr: daily precipitation and 2 m temperature out to 46 (ECMWF, Cumulus) or 35 (GEFS) days, from Sheerwater's tiled hindcast stores. Use when a task needs past forecasts for calibration, bias correction, skill scores or training against observations."
license: MIT
compatibility: "Requires Python 3.12 and uv. Reads Sheerwater's hindcast Zarr stores via gcsfs. ECMWF and GEFS are public (gs://sheerwater-public-datalake/hindcasts) and need no credentials. Cumulus is private (gs://sheerwater-datalake/hindcasts) and needs Google Cloud credentials: NEURAL_GCM_SERVICE_CREDENTIALS (the raw service-account key JSON), GOOGLE_APPLICATION_CREDENTIALS (a service-account JSON file path), or Application Default Credentials from `gcloud auth application-default login`."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - tmp2m
  openclaw:
    envVars:
      - name: NEURAL_GCM_SERVICE_CREDENTIALS
        description: Full contents of a GCP service-account key JSON that can read gs://sheerwater-datalake/hindcasts. Needed for Cumulus only.
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON file that can read gs://sheerwater-datalake/hindcasts. Used only if NEURAL_GCM_SERVICE_CREDENTIALS is unset.
---

# hindcast-fetch

Fetches every hindcast init in a date window from Sheerwater's tiled stores and stacks them on one `time` axis.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf|gefs|cumulus --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o OUT.zarr \
  [-v precip] [-v tmp2m] [--bbox N/W/S/E] [--members]
# Latest hindcast init date (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET]
```

| `--dataset` | Inits | Members | Leads (days) | Grid | Credentials |
| --- | --- | --- | --- | --- | --- |
| `ecmwf` | 1996-01-04 to 2021-12-29, one run per date, from the newest model that ran it | mean of 10, or the 10 perturbed with `--members` (`number` 1–10) | 0–45 | 1.5° | none |
| `gefs` | Wednesdays 1998-12-30 to 2019-08-28 | 11 (`number` 0–10) | 0–34 | 1° | none |
| `cumulus` | every second day, 2016-01-03 to 2024-12-31 | 30 (`number` 0–29) | 0–45 | 1° | Google Cloud |

Every dataset has `precip` and `tmp2m`.

### Arguments
- `--dataset` — `ecmwf`, `gefs` or `cumulus` (the IMERG-trained `gratis-czajkowski` Cumulus run).
- `--start-time`, `--end-time` — inclusive window of hindcast init dates (the dates the runs start from, not when they were issued). Pass the same date twice for one init.
- `--variable`, `-v` — `precip` and/or `tmp2m`, repeatable. Default: `precip`. `tp` and `t2m` are accepted too.
- `--bbox` — `N/W/S/E` decimal degrees. Omit for the full global grid.
- `--members` — `ecmwf` only: the 10 perturbed members instead of their mean. `gefs` and `cumulus` always return every member.
- `--probe-latest` — print the latest hindcast init date (`YYYY-MM-DD`) and exit. Optional value: a `--dataset` id (default `ecmwf`).
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, [number,] step, latitude, longitude)`: `time` is each hindcast's init date, `step` is daily and labels the start of each 24 h window, so `step=0` is `[init, init+1d)`. `ecmwf` without `--members` has no `number` dim.
- `precip` in `mm day-1`, `tmp2m` in `degree_Celsius`. Longitude is `[-180, 180)`.
- `data_interval` `1 day`. Global attrs `weather_skills_source` (the stores read) and `hindcast_dataset`.

### Notes
- `ecmwf` is one time series. ECMWF re-ran each issue's calendar date in each of the 20 years before it, so most dates were run by several issues; the store keeps the newest issue's run of each date. The model cycle still varies from date to date, so a trend across inits can be a model change.
- `precip` is clipped at 0 in every store: ECMWF's daily values come from differenced accumulations, and Cumulus's model output can dip slightly below 0. A store with negative `precip` is reported on stderr and kept as published.
- `cumulus` `tmp2m` is the model's 2 m temperature as published, one value per lead day; the source does not say whether it is a daily mean or the value at the end of the day.
- `gefs` `tmp2m` is one value per day as the source publishes it; the source does not say whether it is a daily mean.
- Stores of one model can skip different inits (GEFS `precip` and `tmp2m` do). A variable is NaN on inits its store lacks, never filled; stderr lists them. The stores' grids must match exactly, or the call exits 1.
- `ecmwf` hindcast inits fall on the same calendar dates in every year, but on any weekday. Pair them with `gefs` Wednesday inits by valid date, not by init.
- Every store is in 6° lat/lon tiles with all inits in each chunk: a small `--bbox` reads only its tiles, and a whole record takes about as long as one init (over Kenya, about a minute). Without `--bbox` the whole globe is read.
- Without the needed credentials the call exits 1 and says which to set.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf --members -v precip -v tmp2m --start-time 1996-01-01 --end-time 2021-12-31 \
  --bbox 5.5/33.5/-5/42.5 -o ecmwf_hindcast_kenya.zarr
```
Writes 10-member daily precipitation and 2 m temperature hindcasts over Kenya for every ECMWF init date from 1996 to 2021.

## Details

| `--dataset` | `precip` | `tmp2m` |
| --- | --- | --- |
| `ecmwf` | `gs://sheerwater-public-datalake/hindcasts/ecmwf_ifs_precip.zarr` | `gs://sheerwater-public-datalake/hindcasts/ecmwf_ifs_tmp2m.zarr` |
| `gefs` | `gs://sheerwater-public-datalake/hindcasts/gefs_precip.zarr` | `gs://sheerwater-public-datalake/hindcasts/gefs_tmp2m.zarr` |
| `cumulus` | `gs://sheerwater-datalake/hindcasts/cumulus_precip.zarr` | `gs://sheerwater-datalake/hindcasts/cumulus_tmp2m.zarr` |

**ECMWF.** Both stores are WeatherBench2's copy of the ECMWF IFS extended-range reforecast members, flattened from (issue date, hindcast year) to one run per real init date, keeping the newest issue where several ran the same date. Dims `(member, prediction_timedelta, lat, lon, init_time)`. The ensemble mean is the mean of the 10 members, as in WeatherBench2's own ensemble-mean store.

**GEFS.** Both stores are Sheerwater's public copies of its CHC GEFSv12 reforecast caches. Dims `(init_time, member, prediction_timedelta, lat, lon)`.

**Cumulus.** Both stores are the `gratis-czajkowski` Cumulus (Aurora S2S) hindcast run at 1°: member 0 is the run from the `cf` files, 1–29 those from the `pf` files. `prediction_timedelta` d holds the model's lead of (d + 1) × 24 h; for `precip` that is the 24 h total ending then, labelled by its start, with negative model output set to 0 as in the live forecast export.

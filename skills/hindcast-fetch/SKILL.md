---
name: hindcast-fetch
description: "Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts (reforecasts) for a range of init dates over a region, as a multi-init weather-skills forecast Zarr: daily precipitation (all three) and 2 m temperature (ECMWF, GEFS) out to 46 (ECMWF, Cumulus) or 35 (GEFS) days. Use when a task needs past forecasts for calibration, bias correction, skill scores or training against observations."
license: MIT
compatibility: "Requires Python 3.12 and uv. ECMWF reads the public WeatherBench2 reforecast in gs://weatherbench2 and the public gs://sheerwater-public-datalake/hindcasts/ecmwf_ifs_precip.zarr anonymously. GEFS precip reads the public gs://sheerwater-public-datalake/hindcasts/gefs_precip.zarr anonymously; GEFS tmp2m reads the private gs://sheerwater-datalake/caches/gefs_chc_raw via gcsfs and needs Google Cloud credentials: NEURAL_GCM_SERVICE_CREDENTIALS (the raw service-account key JSON), GOOGLE_APPLICATION_CREDENTIALS (a service-account JSON file path), or Application Default Credentials from `gcloud auth application-default login`. Cumulus reads private NetCDF files from Azure Blob italynorthdata/data/ml_reforecasts/gratis-czajkowski via adlfs and needs AZURE_STORAGE_SAS_TOKEN (a read SAS with list and read, sp=rl)."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - tmp2m
    - tmax2m
    - tmin2m
    - d2m
    - sst
  openclaw:
    envVars:
      - name: NEURAL_GCM_SERVICE_CREDENTIALS
        description: Full contents of a GCP service-account key JSON with read access to gs://sheerwater-datalake/caches/gefs_chc_raw. Needed for --dataset gefs -v tmp2m only.
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON file that can read gs://sheerwater-datalake/caches/gefs_chc_raw. Used only if NEURAL_GCM_SERVICE_CREDENTIALS is unset.
      - name: AZURE_STORAGE_SAS_TOKEN
        description: Read SAS (list and read, sp=rl) for Azure container italynorthdata/data. Needed for the cumulus datasets only.
---

# hindcast-fetch

Fetches every hindcast init in a date window and stacks them on one `time` axis.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset DATASET --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o OUT.zarr \
  [-v VAR ...] [--bbox N/W/S/E] [--issue-year YYYY] [--members] [--workers N]
# Latest hindcast init date (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET]
```

| `--dataset` | Inits | Members | Leads (days) | Grid | `-v` | Credentials |
| --- | --- | --- | --- | --- | --- | --- |
| `ecmwf` | 1996–2021, the calendar dates of the Mon/Thu issues of 2016–2022 | ensemble mean, or 10 perturbed with `--members` | 0–45 | 1.5° | `precip`, `tmp2m`, `tmax2m`, `tmin2m`, `d2m`, `sst` | none |
| `gefs` | Wednesdays 1998-12-30 to 2019-08-28 | 11 (`number` 0–10) | 0–34 | 1° | `precip`, `tmp2m` | `tmp2m` only: Google Cloud |
| `cumulus` | every second day, 2016-01-01 to 2024-12-31 | 30 (`number` 0–29) | 0–45 | 0.25° | `precip` | Azure SAS |
| `cumulus_1deg` | as `cumulus` | 30 | 0–45 | 1° | `precip` | Azure SAS |
| `cumulus_era5` | as `cumulus` | 30 | 0–45 | 1° | `precip` | Azure SAS |

### Arguments
- `--dataset` — one of the table's ids. `cumulus` and `cumulus_1deg` are the IMERG-trained Cumulus model at 0.25° and 1°; `cumulus_era5` is the ERA5-trained one.
- `--start-time`, `--end-time` — inclusive window of hindcast init dates (the dates the runs start from, not when they were issued). Pass the same date twice for one init.
- `--variable`, `-v` — repeatable. Default: `precip`. `tp` and `t2m` are accepted for `precip` and `tmp2m`.
- `--bbox` — `N/W/S/E` decimal degrees. Omit for the full global grid.
- `--issue-year` — `ecmwf` only: the year (2016–2022) whose issues ran the hindcasts. Each issue year carries the 20 years before it. Default: the latest issue year that covers the whole window, except for `precip` alone from 2016 on (see Notes). For the newest model, pass `--issue-year 2022`: inits 2002–2021, all from 2022 issues.
- `--members` — `ecmwf` only: the 10 perturbed members instead of the ensemble mean.
- `--workers` — Cumulus only: concurrent file downloads, at least 1. Default: 8.
- `--probe-latest` — print the latest hindcast init date (`YYYY-MM-DD`) and exit. Optional value: a `--dataset` id (default `ecmwf`).
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, [number,] step, latitude, longitude)`: `time` is each hindcast's init date, `step` is daily and labels the start of each 24 h window, so `step=0` is `[init, init+1d)`. `ecmwf` without `--members` has no `number` dim.
- `precip` in `mm day-1`; `tmp2m`, `tmax2m`, `tmin2m` (24 h mean, maximum, minimum 2 m temperature), `d2m` and `sst` in `degree_Celsius`. Longitude is `[-180, 180)`.
- `ecmwf` adds an `issue_time` coordinate on `time`: the real-time forecast date each hindcast was run alongside.
- `data_interval` `1 day`. Global attrs `weather_skills_source`, `hindcast_dataset`, and for `ecmwf` with an issue year `hindcast_issue_year`.

### Notes
- `ecmwf` `precip` comes from Sheerwater's tiled public store whenever it holds the runs asked for, and from WeatherBench2 otherwise; stderr names the source and `weather_skills_source` records it. The store holds every init date from 2016-01-01 to 2021-12-29 once, each from the newest issue that ran it. So:
  - `precip` without `--issue-year` and from 2016 on reads every init date in the window from the store. Inits from different years then come from different model cycles, so a trend across years can be a model change; pass `--issue-year` to keep one issue year.
  - With `--issue-year`, `precip` comes from the store when every selected run is the newest for its date (always so for 2022), else from WeatherBench2.
  - The store's `precip` is clipped at 0, and its ensemble mean is the mean of its 10 clipped members (WeatherBench2's ensemble mean is that same mean, unclipped).
  - Other variables always come from WeatherBench2, on the same inits.
- ECMWF upgrades its model during some years, so one issue year can span two model cycles; check ECMWF's IFS cycle history for the year you pick.
- WeatherBench2 stores whole globes, so each init costs about 5 MB per variable (50 MB with `--members`) whatever the `--bbox`: a multi-year window takes minutes. The store is in 6° tiles and reads a small `--bbox` in seconds.
- WeatherBench2 `precip` at lead day 15 can be negative (down to about −50 mm day-1) where the source's accumulations are split. Values are kept as published; stderr names the lead days. Average over a week or more, or drop that step, before using daily values.
- `ecmwf` hindcast inits fall on the same calendar dates in every year, but on any weekday. Pair them with `gefs` Wednesday inits by valid date, not by init.
- The `gefs` `precip` and `tmp2m` stores skip different weeks. A variable is NaN on inits its store lacks, never filled; stderr lists them. Their grids must match exactly, or the call exits 1.
- `gefs` `tmp2m` is one value per day as the source publishes it; the source does not say whether it is a daily mean.
- Cumulus inits are built like the live forecast export: the control (`cf`) file is member 0 and negative model output is set to 0. A missing lead file exits 1; nothing is padded.
- Cumulus reads each init's whole global files (about 5.7 GB at 0.25°) and keeps the `--bbox`, so an init takes minutes; use `cumulus_1deg` when 1° is enough. Without `--bbox` every global init is held in memory: pass `--bbox` for more than one or two inits.
- Without the needed credentials the call exits 1 or 2 and says which to set.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf --issue-year 2022 --members --start-time 2016-01-01 --end-time 2021-12-31 \
  --bbox 5.5/33.5/-5/42.5 -o ecmwf_hindcast_kenya.zarr
```
Writes the 10-member daily precipitation hindcasts of every 2022-issue init from 2016 to 2021 over Kenya.

## Details

**ECMWF.** WeatherBench2's `gs://weatherbench2/datasets/ifs_extended_range/daily/ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr` (or `…-avg.zarr` for members) is the ECMWF IFS extended-range reforecast. It is indexed by issue date (2016-01-04 to 2022-12-29) and hindcast year (−20 to −1), with each pair's real init date in a 2-D `time` coordinate; the skill picks the pairs of one issue year whose real init falls in the window. It carries no units: precipitation is a 24 h total in metres, temperatures are 24 h values in K. `gs://sheerwater-public-datalake/hindcasts/ecmwf_ifs_precip.zarr` is the members' precipitation flattened to one run per real init date: where several issues ran the same date, the newest is kept. Its dims are `(member, prediction_timedelta, lat, lon, init_time)`, in mm, in 6° tiles.

**GEFS.** `precip` reads `gs://sheerwater-public-datalake/hindcasts/gefs_precip.zarr`, Sheerwater's public copy of its GEFSv12 reforecast cache in 6° tiles; `tmp2m` reads the cache itself, `gs://sheerwater-datalake/caches/gefs_chc_raw/global1_0_tmp2m.zarr` (Zarr v3, init times in days since 1960-01-01).

**Cumulus.** Files live at `az://italynorthdata/data/ml_reforecasts/gratis-czajkowski/{cf,pf}/<folder>/data/YYYY-MM-DD-HH-LLLL.nc`, one global file per init and lead (`LLLL` = lead hours, 0024–1104); each lead is the 24 h total ending at init + `LLLL`, relabelled to the period start.

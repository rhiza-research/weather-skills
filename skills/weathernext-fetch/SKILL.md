---
name: weathernext-fetch
description: "Fetch a Google WeatherNext AI forecast for one init over an optional lat/lon box, as a weather-skills Zarr: WeatherNext 2 (64-member ensemble, 6-hourly leads to 15 days, 0.25° global, from 2025) or WeatherNext 3 (64-member ensemble or its precomputed mean and percentiles, hourly leads to 15 days, 0.1° global, from 2026). Precipitation, 2 m temperature and dewpoint, winds, sea-level pressure, SST, cloud, radiation and pressure-level fields. Reads the gs://weathernext buckets with a Google Cloud login. Use when a task needs a WeatherNext forecast."
license: MIT
compatibility: "Requires Python 3.12 and uv. Reads consolidated Zarr from the WeatherNext 2 and 3 realtime archives via gcsfs. Requires Google Cloud credentials: GCLOUD_SERVICE_CREDENTIALS (the raw service-account key JSON, for environments that can only inject secret values), GOOGLE_APPLICATION_CREDENTIALS (a service-account JSON file path), or Application Default Credentials from `gcloud auth application-default login` — any authenticated Google account works, the buckets have no special allowlist; a Google account is free with sign-up at https://accounts.google.com/signup. WeatherNext 3 ensemble reads are requester pays and need a billing project (--billing-project or GOOGLE_CLOUD_PROJECT)."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - tp
    - 2m_temperature
    - 2m_dewpoint_temperature
    - 10m_u_component_of_wind
    - 10m_v_component_of_wind
    - 100m_u_component_of_wind
    - 100m_v_component_of_wind
    - mean_sea_level_pressure
    - sea_surface_temperature
    - total_cloud_cover
    - surface_solar_radiation_downwards_1hr
    - specific_humidity
    - temperature
    - u_component_of_wind
    - v_component_of_wind
    - vertical_velocity
    - geopotential
  openclaw:
    requires:
      env:
        - GCLOUD_SERVICE_CREDENTIALS
    primaryEnv: GCLOUD_SERVICE_CREDENTIALS
    envVars:
      - name: GCLOUD_SERVICE_CREDENTIALS
        description: Full contents of a GCP service-account key JSON (any Google Cloud identity can read the WeatherNext buckets). Use this when the runtime can only inject secret values, not files.
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON file. Used only if GCLOUD_SERVICE_CREDENTIALS is unset.
      - name: GOOGLE_CLOUD_PROJECT
        description: Billing project for requester-pays WeatherNext 3 ensemble reads (alternative to --billing-project)
---

# weathernext-fetch

Fetches one WeatherNext forecast init as a weather-skills standard dataset Zarr.

| `--version` / `--product` | Store | Members | Leads | Grid | Cost |
|---|---|---|---|---|---|
| `2` (default) | `gs://weathernext/weathernext_2_0_0/zarr/2025_to_present/` | 64 | 60 × 6 h | 0.25° | free |
| `3` | `gs://weathernext3_spatial/weathernext_3_0_0/zarr/2026_to_present/` | 64 | 360 × 1 h | 0.1° surface, 0.05° station-head, 0.25° upper air | requester pays |
| `3 --product statistics` | `gs://weathernext3_statistics_spatial/weathernext_3_0_0_statistics/zarr/2026_to_present/` | mean, p10/p25/p50/p75/p90 | 360 × 1 h | 0.1° / 0.05° surface | free |

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 2|3] [--product ensemble|statistics] [--date YYYY-MM-DD] [--cycle HH] [--bbox N/W/S/E] -v VAR [-v VAR ...] [--member N ...] [--statistic STAT ...] [--max-lead 72h] [--billing-project PROJECT] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 3 ...] --probe-latest [--cycle HH]
```

Needs Google Cloud credentials: `GCLOUD_SERVICE_CREDENTIALS` (key JSON), `GOOGLE_APPLICATION_CREDENTIALS` (key file path), or `gcloud auth application-default login`. WeatherNext 3 ensemble reads are requester pays and exit 2 without a billing project.

### Arguments
- `--version` — `2` (default) or `3`.
- `--product` — WeatherNext 3 only: `ensemble` (default) or `statistics` (precomputed mean and percentiles).
- `--date` — init date. Default: the latest published 00/06/12/18 init.
- `--cycle` — init hour UTC (`6` or `06`). WeatherNext 2: 00/06/12/18; WeatherNext 3 adds 48 h interim inits at the other hours. Default: `00` with `--date`, else the newest 00/06/12/18.
- `--bbox` — `N/W/S/E` decimal degrees. Default: global.
- `--variable`, `-v` — repeatable. Common: `tp`/`precip`, `t2m`, `d2m`, `u10`, `v10`, `msl`, `sst`, `ssrd`; pressure levels `t`, `u`, `v`, `w`, `q`, `z` (more under Details). With `--product statistics`, `<name>_<stat>` (`tp_p90`) picks one statistic. Default: WeatherNext 2 every field; WeatherNext 3 every 0.1° surface field. Unknown names exit 2 listing `Available:`.
- `--member` — 0-based member index, repeatable. Default: all 64. Not with `--product statistics`.
- `--statistic` — with `--product statistics`: `mean`, `p10`, `p25`, `p50`, `p75`, `p90`, repeatable. Default: all six.
- `--max-lead` — keep only leads up to this one after init: hours (`72`) or a timedelta (`72h`, `3D`). Default: every lead.
- `--billing-project` — GCP project billed for WeatherNext 3 ensemble reads. Default: `$GOOGLE_CLOUD_PROJECT`, then `$CLOUDSDK_CORE_PROJECT`.
- `--probe-latest [IDENT]` — print the latest init date (or `none`) and exit; honours `--version`, `--product`, `--cycle`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(number, step, [level,] lat, lon)`, scalar `time` = init, `valid_time` on `step`. Statistics have no `number` and name variables `<name>_<stat>`.
- `tp` (and `imerg_tp`, `experimental_tp`) is a rate in `mm day-1` and `ssrd` a 1 h total in `J m-2`, labelled at period start. Other fields are instants at `init + step`. Fetched together, the step axis starts at 0: the period fields are NaN at the last lead and the instants at 0 h. Never `deaccumulate` it.
- Air temperatures are `degree_Celsius`; other fields keep ERA5-style units.

### Notes
- `--bbox` cuts the output, not the download (chunks are global); `-v`, `--member`, `--statistic`, `--max-lead` cut the read.
- WeatherNext 3 fields sit on four grids and lead axes (Details); mixing them in one run exits 2.
- If the request doesn't say, ask the user which version and how many members, with the size and cost from Details.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-14 --cycle 00 --bbox 5.5/33.5/-5/42.5 \
  -v tp -v t2m --member 0 --member 1 --member 2 --member 3 -o weathernext_kenya.zarr
```
Writes 4 WeatherNext 2 members of 6-hourly precip and 2 m temperature over Kenya.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --version 3 --product statistics --bbox 5.5/33.5/-5/42.5 \
  -v tp --statistic mean --statistic p90 --max-lead 72h -o wn3_kenya_tp_stats.zarr
```
Writes WeatherNext 3 hourly mean and 90th-percentile precip over Kenya for 3 days.

## Details

### Variables
- Aliases for both versions: `tp`/`precip`/`pr`, `t2m`, `d2m`, `u10`, `v10`, `u100`, `v100`, `msl`, `sst`; pressure levels `t`, `u`, `v`, `w`, `q`, `z`/`geopotential`.
- WeatherNext 3 adds `tcc`, `lcc`, `mcc`, `hcc`, `ssrd`, `si10`/`si100` (statistics only), `z300`, `z500`, `t300`, `t500`, `u1000`, `v1000`, `imerg_tp`, `experimental_tp`, `total_sky_direct_solar_radiation_at_surface_1hr`, and the 0.05° `station_head_temperature_2m` / `station_head_dewpoint_temperature_2m`.
- Native names also work (`total_precipitation_1hr`, `total_precipitation_1hr_p90`). WeatherNext 3 fields shared with WeatherNext 2 take the same ERA5-style names (`2m_temperature`, `10m_u_component_of_wind`, …).
- A statistic named in `-v` (`tp_p90`) keeps only that statistic; a plain name (`tp`) keeps every statistic `--statistic` allows. A `-v` statistic that `--statistic` excludes exits 2.

### Grids and leads
- `number` is 0..63. `level` is 13 pressure levels, 50–1000 hPa. WeatherNext 3's `lead_time` × `lead_subtime` becomes one hourly `step`, and its `lat_0p1`/`lon_0p1` (etc.) become `lat`/`lon`.
- WeatherNext 3 groups: 0.1° hourly surface, 0.05° hourly station-head, 0.25° hourly single-level upper air (`z500`, `t500`, …) and 0.25° 6-hourly pressure-level cubes (`t`, `u`, `v`, `w`, `q`, `z`).
- Period fields: `step=0` is `[init, init+6h)` for WeatherNext 2 `tp` and `[init, init+1h)` for WeatherNext 3 `tp`, `ssrd` and `total_sky_direct_solar_radiation_at_surface_1hr`. Precipitation is clipped at zero. The radiation fields stay hourly sums in `J m-2` (divide by 3600 s for a mean `W m-2` flux).

### Flags
- WeatherNext 2 inits run daily from 2025-01-01, WeatherNext 3 from 2026. WeatherNext 3 interim cycles are `01`–`05`, `07`–`11`, `13`–`17`, `19`–`23`.
- `--bbox` takes negative longitudes. A negative `--member` counts from the end.
- Anonymous access fails; any authenticated Google identity can read the buckets. `GCLOUD_SERVICE_CREDENTIALS` (the service-account key JSON itself, no file needed) takes precedence over `GOOGLE_APPLICATION_CREDENTIALS` and gcloud login. The WeatherNext 3 ensemble bucket bills every listing and read to the billing project, and the skill exits 2 without one before touching GCS. gcloud's default project (`gcloud config set project`) is not read: pass `--billing-project` or export `GOOGLE_CLOUD_PROJECT`.

### Units and provenance
- SST stays `K`; others keep ERA5-style units (`m s-1`, `Pa`, `Pa s-1`, `kg kg-1`, `m2 s-2`, `J m-2`, cloud fraction `1`).
- Stamped `weather_skills_source=weathernext:<init stamp>`, `weathernext3:<init stamp>` or `weathernext3-statistics:<init stamp>`.

### Size and cost
- Every read downloads whole chunks, and each chunk is the full globe for one member and one 6 h lead (WeatherNext 2) or 6 h lead block (WeatherNext 3). Estimate the download as fields × members × 6 h blocks × chunk size.
- Compressed chunk sizes, measured Oct 2026: WeatherNext 2 ~2–3.5 MB; WeatherNext 3 0.1° surface field ~19 MB. One field, 64 members, 72 h (12 blocks) is ~2 GB from WeatherNext 2 and ~15 GB from WeatherNext 3. A whole field for 15 days is ~9–13 GB (WeatherNext 2) or ~290 GB (WeatherNext 3).
- WeatherNext 2 and WeatherNext 3 statistics are free to read. WeatherNext 3 ensemble reads are billed to `--billing-project` as GCS egress, about $0.12 per GB outside Google Cloud: ~$1.80 for the 15 GB example, ~$35 for a whole 15-day field. Say the estimate before fetching.
- Only the per-init realtime stores are read; the older multi-init WeatherNext 2 stores and WeatherNext 3 backfill stores are not.

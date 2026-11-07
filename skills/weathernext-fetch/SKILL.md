---
name: weathernext-fetch
description: "Fetch a Google WeatherNext AI forecast for one init or a range of inits over an optional lat/lon box, as a weather-skills Zarr: WeatherNext 2 (64-member ensemble, 6-hourly leads to 15 days, 0.25° global, from 2022) or WeatherNext 3 (64-member ensemble or its precomputed mean and percentiles, hourly leads to 15 days, 0.1° global, from 2026). Precipitation, 2 m temperature (and dewpoint, WeatherNext 3 only), winds, sea-level pressure, SST, cloud, radiation and pressure-level fields. WeatherNext 2 surface fields come from dynamical.org with no login; everything else reads Google Cloud with a Google login. Use for any WeatherNext forecast."
license: MIT
compatibility: "Requires Python 3.12 and uv. WeatherNext 2 surface fields from dynamical.org need no credentials. Everything else reads Google Cloud and needs Google credentials: GCLOUD_SERVICE_CREDENTIALS (the raw service-account key JSON, for environments that can only inject secret values), GOOGLE_APPLICATION_CREDENTIALS (a key file path), or `gcloud auth application-default login`. Any authenticated Google account works (free at https://accounts.google.com/signup). WeatherNext 3 ensemble reads are requester pays and need a billing project (--billing-project or GOOGLE_CLOUD_PROJECT)."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.11"
  catalog-group: fetchers
  variables:
    - precip
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
    envVars:
      - name: GCLOUD_SERVICE_CREDENTIALS
        description: Full contents of a GCP service-account key JSON (any Google account can read WeatherNext). Use this when the runtime can only inject secret values, not files. Needed only for Google Cloud reads (WeatherNext 3, and WeatherNext 2 fields dynamical.org lacks).
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON file. Used only if GCLOUD_SERVICE_CREDENTIALS is unset.
      - name: GOOGLE_CLOUD_PROJECT
        description: Billing project for requester-pays WeatherNext 3 ensemble reads (alternative to --billing-project)
---

# weathernext-fetch

Fetches one WeatherNext forecast init, or every init in one or more date windows, as a weather-skills standard dataset Zarr.

| `--version` / `--product` | Source | Inits | Members | Leads | Grid | Cost |
|---|---|---|---|---|---|---|
| `2` (default) | dynamical.org for its fields (no login), otherwise Google Cloud | from 2022 on dynamical.org, 2025 on Google Cloud | 64 | 60 × 6 h | 0.25° | free |
| `3` (`--product ensemble`, default) | Google Cloud | from 2026 | 64 | 360 × 1 h | 0.1° surface, 0.05° station-head, 0.25° upper air | requester pays |
| `3 --product statistics` | Google Cloud | from 2026 | mean, p10/p25/p50/p75/p90 | 360 × 1 h | 0.1° / 0.05° surface | free |

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 2|3] [--product ensemble|statistics] [--date YYYY-MM-DD] [--cycle HH] [--bbox N/W/S/E] -v VAR [-v VAR ...] [--member N ...] [--statistic STAT ...] [--max-lead 72h] [--billing-project PROJECT] -o OUT.zarr
# Every 00 UTC (or --cycle) init in a window, in one call; repeat the pair for several windows
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 2|3 ...] --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--start-time ... --end-time ...] [--cycle HH] --bbox N/W/S/E -v VAR [--member N ... | --statistic STAT ...] --max-lead 240h -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 3 ...] --probe-latest [--cycle HH]
```

WeatherNext 2 picks its source per request, and one stderr line (`Source: ...`) names it and, for Google Cloud, why:
- dynamical.org, at any cycle (00/06/12/18 UTC), when every `-v` is one of `precip`, `t2m`, `sst`, `msl`, `u10`, `v10`, `u100`, `v100`. No credentials needed.
- Google Cloud otherwise (pressure levels, or no `-v`), or when dynamical.org fails or lacks a complete init for the date (for windows: lacks every init in them). With windows, each init dynamical.org lacks or is still publishing is skipped with a `WARNING` naming it (not filled, not read from Google Cloud).
- Inits before 2025 exist only on dynamical.org: a request that does not fit it, or a dynamical.org failure, exits 1.

Google Cloud reads need credentials: `GCLOUD_SERVICE_CREDENTIALS` (key JSON, takes precedence), `GOOGLE_APPLICATION_CREDENTIALS` (key file path), or `gcloud auth application-default login`; anonymous access fails. WeatherNext 3 always reads Google Cloud; its ensemble reads are billed to a billing project, and without one the skill exits 2 before reading. gcloud's default project is not read: pass `--billing-project` or export `GOOGLE_CLOUD_PROJECT`.

### Arguments
- `--version` — `2` (default) or `3`.
- `--product` — WeatherNext 3 only: `ensemble` (default) or `statistics` (precomputed mean and percentiles).
- `--date` — init date. Default: the latest published 00/06/12/18 init.
- `--start-time`, `--end-time` — an init window, both days inclusive. Repeat the pair for several windows; not with `--date`. Takes the `--cycle` init (default 00 UTC) of every published day in the windows with all its leads: the windows select init times, not valid times. An init not yet complete is skipped with a `WARNING`; no init in any window exits 1.
- `--cycle` — init hour UTC (`6` or `06`). WeatherNext 2: 00/06/12/18; WeatherNext 3 adds 48 h interim inits at the other hours (`01`–`05`, `07`–`11`, `13`–`17`, `19`–`23`). Default: `00` with `--date` or windows, else the newest 00/06/12/18.
- `--bbox` — `N/W/S/E` decimal degrees, negative longitudes allowed. Default: global.
- `--variable`, `-v` — repeatable; see [Variables](#variables). With `--product statistics`, `<name>_<stat>` (`precip_p90`) picks one statistic. Default: WeatherNext 2 every field; WeatherNext 3 every 0.1° surface field. Unknown names exit 2 listing `Available:`.
- `--member` — 0-based member index, repeatable; negative counts from the end. Default: all 64. Not with `--product statistics`.
- `--statistic` — with `--product statistics`: `mean`, `p10`, `p25`, `p50`, `p75`, `p90`, repeatable. Default: all six.
- `--max-lead` — keep only leads up to this one after init: hours (`72`) or a timedelta (`72h`, `3D`). Default: every lead.
- `--billing-project` — GCP project billed for WeatherNext 3 ensemble reads. Default: `$GOOGLE_CLOUD_PROJECT`, then `$CLOUDSDK_CORE_PROJECT`.
- `--probe-latest [IDENT]` — print the latest init date (or `none`) and exit; honours `--version`, `--product`, `--cycle`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- The same whichever WeatherNext 2 source served it. Dims `(number, step, [level,] lat, lon)`, scalar `time` = init, `valid_time` on `step`. With windows, `(time, step, number, [level,] lat, lon)`, one `time` per init, and `valid_time` on `(time, step)`; an init still publishing keeps its missing leads NaN (not filled), with a `WARNING` naming it. Statistics have no `number` and name variables `<name>_<stat>`.
- `number` is 0..63; `level` is 13 pressure levels, 50–1000 hPa. WeatherNext 3 leads form one hourly `step`.
- `precip` (and `imerg_precip`, `experimental_precip`) is a rate in `mm day-1`, clipped at zero; `ssrd` and `total_sky_direct_solar_radiation_at_surface_1hr` are 1 h totals in `J m-2` (divide by 3600 s for `W m-2`). These are labelled at period start: `step=0` is `[init, init+6h)` for WeatherNext 2 `precip`, `[init, init+1h)` for WeatherNext 3. Other fields are instants at `init + step`. Fetched together, the period fields are NaN at the last lead and the instants at 0 h. Never `deaccumulate` it.
- Air temperatures are `degree_Celsius`; SST stays `K`; others keep ERA5-style units (`m s-1`, `Pa`, `Pa s-1`, `kg kg-1`, `m2 s-2`, `J m-2`, cloud fraction `1`).
- `weather_skills_source` names the version, product and init(s).

### Notes
- `--bbox` cuts the output, not the download: every read is the whole globe, so a one-country fetch downloads as much as a global one and takes as long. `-v`, `--member`, `--statistic` and `--max-lead` do cut the read. Fetch only the fields, members and leads the task needs, and tell the user before a large read that it will be slow.
- WeatherNext 3 fields sit on four grids and lead axes ([Variables](#variables)); mixing them in one run exits 2.
- If the request doesn't say, ask the user which version and how many members, with the size and cost below.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-14 --cycle 00 --bbox 5.5/33.5/-5/42.5 \
  -v precip -v t2m --member 0 --member 1 --member 2 --member 3 -o weathernext_kenya.zarr
```
Writes 4 WeatherNext 2 members of 6-hourly precip and 2 m temperature over Kenya.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --version 3 --product statistics --bbox 5.5/33.5/-5/42.5 \
  -v precip --statistic mean --statistic p90 --max-lead 72h -o wn3_kenya_precip_stats.zarr
```
Writes WeatherNext 3 hourly mean and 90th-percentile precip over Kenya for 3 days.

## Details

### Variables
- Aliases for both versions: `precip`/`tp`/`precipitation`/`pr`, `t2m`, `d2m` (WeatherNext 3 only; WeatherNext 2 has no dewpoint), `u10`, `v10`, `u100`, `v100`, `msl`, `sst`; pressure levels `t`, `u`, `v`, `w`, `q`, `z`/`geopotential`.
- WeatherNext 3 adds `tcc`, `lcc`, `mcc`, `hcc`, `ssrd`, `si10`/`si100` (statistics only), `z300`, `z500`, `t300`, `t500`, `u1000`, `v1000`, `imerg_precip`, `experimental_precip`, `total_sky_direct_solar_radiation_at_surface_1hr`, and the 0.05° `station_head_temperature_2m` / `station_head_dewpoint_temperature_2m`.
- WeatherNext 3 groups (one per run): 0.1° hourly surface, 0.05° hourly station-head, 0.25° hourly single-level upper air (`z500`, `t500`, …) and 0.25° 6-hourly pressure levels (`t`, `u`, `v`, `w`, `q`, `z`).
- Native names also work (`total_precipitation_1hr`, `total_precipitation_1hr_p90`); fields shared with WeatherNext 2 take the same ERA5-style names (`2m_temperature`, `10m_u_component_of_wind`, …).
- A statistic named in `-v` (`precip_p90`) keeps only that statistic; a plain name (`precip`) keeps every statistic `--statistic` allows. A `-v` statistic that `--statistic` excludes exits 2.

### Size and cost
- The download is about fields × members × 6 h of leads × a per-block size (measured Oct 2026): WeatherNext 2 ~2–3.5 MB, WeatherNext 3 0.1° surface ~19 MB. WeatherNext 2 inits from 2022–2024 download members in fours, so one member costs as much as four.
- One field, 64 members, 72 h is ~2 GB from WeatherNext 2 and ~15 GB from WeatherNext 3. A whole field for 15 days is ~9–13 GB (WeatherNext 2) or ~290 GB (WeatherNext 3).
- WeatherNext 2 and WeatherNext 3 statistics are free. WeatherNext 3 ensemble reads are billed to `--billing-project` at about $0.12 per GB outside Google Cloud: ~$1.80 for the 15 GB example, ~$35 for a whole 15-day field. Say the estimate before fetching.
- Windows multiply all of this by the number of inits, and memory grows with inits × members × steps × box cells. With windows always pass `-v`, `--member` (or `--statistic`) and `--max-lead`: WeatherNext 2, one field, 4 members to 10 days is ~0.4 GB per init, so a month of daily inits is ~13 GB. Keep a WeatherNext 2 window to about a month at that size, and say the estimate before fetching.

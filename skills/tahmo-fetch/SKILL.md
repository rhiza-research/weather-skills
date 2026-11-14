---
name: tahmo-fetch
description: "Fetch daily TAHMO weather-station observations (rainfall, mean, max and min temperature, humidity, pressure) from the Trans-African Hydro-Meteorological Observatory network, for chosen station ids or every station in a lat/lon box, as a weather-skills station Zarr. Can also list the stations in a box, or with --gridded read Sheerwater's historical daily TAHMO rainfall grid (0.25° cell means, 2015-2025) from private GCS. Needs TAHMO credentials (station mode) or Google Cloud credentials (--gridded). Use when a task needs in-situ station data in Africa, for example to compare against gridded satellite or forecast data."
license: MIT
compatibility: Requires Python 3.12 and uv. Installs the TAHMO Python SDK from GitHub (git+https://github.com/rhiza-research/tahmo-api). Station mode needs TAHMO_API_USERNAME and TAHMO_API_PASSWORD; --gridded needs Google Cloud credentials.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.10"
  catalog-group: fetchers
  variables:
    - precip
    - temperature
    - tmax
    - tmin
    - humidity
    - pressure
  openclaw:
    requires:
      env:
        - TAHMO_API_USERNAME
        - TAHMO_API_PASSWORD
    primaryEnv: TAHMO_API_USERNAME
    envVars:
      - name: GCLOUD_SERVICE_CREDENTIALS
        description: --gridded only. Full contents of a GCP service-account key JSON with read access to gs://sheerwater-datalake/hindcasts. Use this when the runtime can only inject secret values, not files.
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: --gridded only. Path to a GCP service-account JSON file that can read gs://sheerwater-datalake/hindcasts. Used only if GCLOUD_SERVICE_CREDENTIALS is unset.
---

# tahmo-fetch

Lists TAHMO stations in a box, or fetches their quality-controlled records and writes daily values as one station Zarr. With `--gridded`, reads Sheerwater's historical daily rainfall grid built from the same stations instead.

## Usage

```bash
# List stations in a box (TSV on stdout, no -o)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox N/W/S/E
# Fetch chosen stations, or every TA station in a box
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py (--station ID [--station ID ...] | --bbox N/W/S/E) --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--day-start-hour H] [--workers N] -o OUT.zarr
# Historical gridded rainfall (0.25° cells), no TAHMO login
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --gridded --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o OUT.zarr
# Print the latest day with a complete record, and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--station ID | --bbox N/W/S/E]
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest --gridded --start-time YYYY-MM-DD --end-time YYYY-MM-DD
```

### Arguments
- `--list-stations` — print the TA stations in `--bbox` as TSV (`station_id`, `name`, `latitude`, `longitude`, `country` as ISO 3166-1 alpha-2) and exit. Requires `--bbox`.
- `--station` — station id such as `TA00025`; repeat for several. When given, `--bbox` is not used to select stations. An unknown id exits 2.
- `--bbox` — `N/W/S/E` decimal degrees. Without `--station`, selects every station whose id starts with `TA` inside the box. W > E crosses the antimeridian.
- `--start-time`, `--end-time` — inclusive range of days, `YYYY-MM-DD`.
- `--day-start-hour` — UTC hour each day starts at, 0-23. Default: 0 (UTC calendar days). Day D runs from D HH:00 to D+1 HH:00 UTC and is labelled D. KMSA uses 6: 06 UTC 2 Oct to 06 UTC 3 Oct is 2 Oct's value.
- `--workers` — concurrent station downloads. Default: 8. Lower it if TAHMO throttles.
- `--gridded` — read the historical gridded store instead of the TAHMO API (see below). Prefer it for long historical rainfall requests (months to years, 2015–2025): it is much faster than downloading each station (all of Kenya for 2015–2025 takes seconds). Takes `--start-time`, `--end-time` and optional `--bbox`; `--station`, `--list-stations` and a `--day-start-hour` other than 0 exit 2. `--workers` is ignored.
- `--probe-latest` — print the latest day with a complete record in the last 14 days for the `--station` / `--bbox` stations (default: the first three TA stations) and exit. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)` with coords `latitude`, `longitude`, `name`, `country` on `station_id`. `featureType="timeSeries"`, stamped `data_interval = "1 day"`.
- `precip` (`mm day-1`, daily sum), `temperature` (`degree_Celsius`, daily mean), `tmax` / `tmin` (`degree_Celsius`, daily maximum / minimum of the 5-minute temperatures, `cell_methods` `time: maximum` / `time: minimum`), `humidity` (fraction, units `1`, daily mean), `pressure` (`kPa`, daily mean) — whichever the stations report.
- `time` holds every requested day, labelled by the date each day starts on. Stations with no data are left out.
- Only records with TAHMO quality flag <= 2 are used; where several sensors report the same variable at the same time, the best-flagged one is kept.
- With `--gridded`: `precip` (`mm day-1`) with dims `(time, latitude, longitude)` on a regular 0.25° grid (cell centres at multiples of 0.25°) over the cells the store covers, cut to `--bbox`. Cells with no station are NaN. `weather_skills_source=sheerwater:hindcasts/tahmo_precip.zarr`, `data_interval` `1 day`.

### Daily values: complete records only
- TAHMO records every 5 minutes and stamps each record at the end of its interval. With `--day-start-hour H`, day D holds the 288 stamps from D H:05 to D+1 H:00, so the H:00 stamp counts toward the day before. With the default H = 0, a `2026-01-02T00:00Z` record counts toward 2026-01-01; with H = 6, a `2026-01-02T06:00Z` record does. The `time` coordinate's `comment` records the day boundary.
- A daily value is NaN unless all 288 records of that day passed the quality filter. One missing record is enough. This holds for every variable: a partial day is not 0 mm or a scaled-up sum, and a partial-day mean, maximum or minimum may miss the afternoon peak or the night low.
- Each variable is judged on its own records; `tmax` and `tmin` share `temperature`'s, so the three are NaN on the same days.

### Notes
- Set `TAHMO_API_USERNAME` and `TAHMO_API_PASSWORD` in the environment for every call, including `--list-stations` and `--probe-latest`; without them it exits 2.
- Stations often miss days. Never fill or extrapolate them. For multi-day totals, a station missing any day of the period must come out NaN; report how many stations were complete. stderr prints each station's complete days and, per variable, how many stations are complete for the whole range (`tmax: 30 of 52 stations complete`).
- A station whose fetch fails is dropped with a stderr line. No data for any station exits 1.

### Gridded history (`--gridded`)
- Use it first for long historical rainfall requests; use station mode for recent days, other variables, individual stations, or when only complete days will do.
- Reads `gs://sheerwater-datalake/hindcasts/tahmo_precip.zarr`. Days run 2015-03-05 to 2025-09-15 (first data 2015-03-24); the store is not updated, so use station mode for recent days. `--probe-latest --gridded` prints its last day with data. A `--bbox` with no cell that has data, or a range with no data in it, exits 1.
- How the grid was made. The store is an unchanged copy (cast to float32) of the TAHMO release `v0.0.0` product `gridded_data_gold/tahmo_gridded_global0_25_mean.zarr`:
  1. **Station-day**: the sum of the station's 5-minute "gold" (quality-controlled) rainfall records on that UTC calendar date. Days with missing records are kept, with no completeness threshold, so a station-day can undercount. For example, 65 of station TA00001's 2,245 days have fewer than the 288 records of a full day (the fewest is 231).
  2. **Station to cell**: each station goes to the 0.25° cell whose centre (a multiple of 0.25°) is nearest its listed location.
  3. **Cell-day**: the plain, unweighted mean of the stations in the cell that have a value that day. The number of stations can change from day to day; 89% of cell-days rest on one station, and the most is 6. The store keeps no station count or completeness per cell. A cell with no station that day is NaN.
  4. **Coverage**: only 23.75–48.5°E, 29.75°S–10.25°N (East and Southern Africa), from 287 of the 603 stations in the release's daily data. West African stations (Ghana, Nigeria, Mali and others) are not in it, nor are 32 stations inside that area (mostly South Africa and Zambia); the release does not say why.
- Station mode differs: it keeps only complete days and reports each station separately. When complete days or individual stations matter, use station mode.
- Credentials: `GCLOUD_SERVICE_CREDENTIALS` (the key JSON itself), else `GOOGLE_APPLICATION_CREDENTIALS` or Application Default Credentials. Without access it exits 1 and says what to set. No TAHMO login is needed.
- stderr prints how many cells have data and how many are complete for the whole range. Never fill the NaN days.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 5.5/33.5/-5/42.5
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station TA00025 --station TA00026 --start-time 2026-09-01 --end-time 2026-09-30 -o tahmo_kenya.zarr
```
Lists Kenya's TAHMO stations, then writes daily September 2026 values for two of them.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --gridded --start-time 2024-03-01 --end-time 2024-05-31 --bbox 5.5/33.5/-5/42.5 -o tahmo_grid_kenya_mam2024.zarr
```
Writes the March-May 2024 gridded TAHMO rainfall over Kenya (62 cells with data).

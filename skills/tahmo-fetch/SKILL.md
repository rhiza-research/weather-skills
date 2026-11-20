---
name: tahmo-fetch
description: "Fetch daily TAHMO weather-station observations (rainfall, mean, max and min temperature, humidity, pressure) from the Trans-African Hydro-Meteorological Observatory network, for chosen station ids or every station in a lat/lon box (2015 to present), as a weather-skills station Zarr. Can also list the stations in a box, or with --gridded read Sheerwater's historical daily all-Africa TAHMO rainfall grid (0.25° cell means of complete station days, 2015-2025) from private GCS. Needs TAHMO credentials (station mode) or Google Cloud credentials (--gridded). Use when a task needs in-situ station data in Africa, for example to compare against gridded satellite or forecast data."
license: MIT
compatibility: Requires Python 3.12 and uv. Installs the TAHMO Python SDK from GitHub (git+https://github.com/rhiza-research/tahmo-api). Station mode needs TAHMO_API_USERNAME and TAHMO_API_PASSWORD; --gridded needs Google Cloud credentials.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.12"
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

Lists TAHMO stations in a box, or fetches their quality-controlled records and writes daily values as one station Zarr. With `--gridded`, reads Sheerwater's historical all-Africa daily rainfall grid built from the same stations instead.

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

### Date range
- Station mode: the network's earliest records are from March 2015 (the gridded store starts 2015-03-05); stations were added over the years, so each station's record starts later and often has gaps. `--list-stations` gives no dates; a fetch prints each station's complete days and leaves out stations with no data. Records are 5-minute, written as daily values. `--probe-latest` gives the newest complete day (it looks back 14 days).
- `--gridded`: 2015-03-05 to 2025-09-13, daily, not updated (see below).

### Arguments
- `--list-stations` — print the TA stations in `--bbox` as TSV (`station_id`, `name`, `latitude`, `longitude`, `country` as ISO 3166-1 alpha-2) and exit. Requires `--bbox`.
- `--station` — station id such as `TA00025`; repeat for several. When given, `--bbox` is not used to select stations. An unknown id exits 2.
- `--bbox` — `N/W/S/E` decimal degrees. Without `--station`, selects every station whose id starts with `TA` inside the box. W > E crosses the antimeridian.
- `--start-time`, `--end-time` — inclusive range of days, `YYYY-MM-DD`.
- `--day-start-hour` — UTC hour each day starts at, 0-23. Default: 0 (UTC calendar days). Day D runs from D HH:00 to D+1 HH:00 UTC and is labelled D. KMSA uses 6: 06 UTC 2 Oct to 06 UTC 3 Oct is 2 Oct's value.
- `--workers` — concurrent station downloads. Default: 8. Lower it if TAHMO throttles.
- `--gridded` — read the historical gridded store instead of the TAHMO API (see below). Prefer it for long historical rainfall requests (months to years, 2015–2025): it is much faster than downloading each station (a whole country for 2015–2025 takes seconds). Takes `--start-time`, `--end-time` and optional `--bbox`; `--station`, `--list-stations` and a `--day-start-hour` other than 0 exit 2. `--workers` is ignored.
- `--probe-latest` — print the latest day with a complete record in the last 14 days for the `--station` / `--bbox` stations (default: the first three TA stations) and exit. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)` with coords `latitude`, `longitude`, `name`, `country` on `station_id`. `featureType="timeSeries"`, stamped `data_interval = "1 day"`.
- `precip` (`mm day-1`, daily sum), `temperature` (`degree_Celsius`, daily mean), `tmax` / `tmin` (`degree_Celsius`, daily maximum / minimum of the 5-minute temperatures, `cell_methods` `time: maximum` / `time: minimum`), `humidity` (fraction, units `1`, daily mean), `pressure` (`kPa`, daily mean) — whichever the stations report.
- `time` holds every requested day, labelled by the date each day starts on. Stations with no data are left out.
- Only records with TAHMO quality flag <= 2 are used; where several sensors report the same variable at the same time, the best-flagged one is kept.
- With `--gridded`: `precip` (`mm day-1`) with dims `(time, latitude, longitude)` on a regular 0.25° grid (cell centres at multiples of 0.25°) over the cells the store covers, cut to `--bbox`, with coordinates `precip_count` (stations averaged per cell-day, 0 where none) and `precip_flag` (quality bit mask, below). Cells with no station are NaN. `weather_skills_source=sheerwater:hindcasts/tahmo_africa_precip.zarr`, `data_interval` `1 day`.

### Daily values: complete records only
- TAHMO records every 5 minutes and stamps each record at the end of its interval. With `--day-start-hour H`, day D holds the 288 stamps from D H:05 to D+1 H:00, so the H:00 stamp counts toward the day before. With the default H = 0, a `2026-01-02T00:00Z` record counts toward 2026-01-01; with H = 6, a `2026-01-02T06:00Z` record does. The `time` coordinate's `comment` records the day boundary.
- A daily value is NaN unless all 288 records of that day passed the quality filter. One missing record is enough. This holds for every variable: a partial day is not 0 mm or a scaled-up sum, and a partial-day mean, maximum or minimum may miss the afternoon peak or the night low.
- Each variable is judged on its own records; `tmax` and `tmin` share `temperature`'s, so the three are NaN on the same days.

### Notes
- Set `TAHMO_API_USERNAME` and `TAHMO_API_PASSWORD` in the environment for every call, including `--list-stations` and `--probe-latest`; without them it exits 2.
- Stations often miss days. Never fill or extrapolate them. For multi-day totals, a station missing any day of the period must come out NaN; report how many stations were complete. stderr prints each station's complete days and, per variable, how many stations are complete for the whole range (`tmax: 30 of 52 stations complete`).
- A station whose fetch fails is dropped with a stderr line. No data for any station exits 1.

### Gridded history (`--gridded`)
- Use it first for long historical rainfall requests; use station mode for recent days, other variables, or individual stations.
- Reads `gs://sheerwater-datalake/hindcasts/tahmo_africa_precip.zarr`. Days run 2015-03-05 to 2025-09-13; the store is not updated, so use station mode for recent days. `--probe-latest --gridded` prints its last day with data. A `--bbox` with no cell that has data, or a range with no data in it, exits 1.
- Coverage: all of Africa that TAHMO covers, 17.5°W–48.5°E and 29.75°S–15.5°N, 475 cells from 587 stations (West Africa included: Ghana, Nigeria, Mali, Togo, Benin, Burkina Faso and others).
- How the grid was made, from the TAHMO release `v0.0.0` 5-minute "gold" station files (records with `final_quality_flag` > 0.9):
  1. **Station-day**: the sum of the day's 288 five-minute rainfall records, D 00:05 to D+1 00:00 UTC (stamps mark the end of each interval, as in station mode). A day with any record missing is dropped, never summed into an undercount. The 13 stations that report every 10–60 minutes never have 288 records, so they are not in the grid.
  2. **Station to cell**: each station goes to the 0.25° cell whose centre (a multiple of 0.25°) is nearest its listed location.
  3. **Cell-day**: the plain, unweighted mean of the stations in the cell with a complete day. `precip_count` gives how many, up to 8; most cell-days rest on one station. A cell with no complete station day is NaN.
- **Quality flags (`precip_flag`)**: suspect station-days are kept in `precip` but flagged. A CF bit mask (`flag_masks` 1, 2, 4) sets each bit if any station averaged into the cell-day has it; 0 means no flag. 271 of 669,899 cell-days carry one.
  - `1` **unsupported_extreme**: over 150 mm, or a 5-minute record over 25 mm, with CHIRPS under 25% of it (largest value within 0.05° over the day before to the day after) and no station within 0.3° reporting even half of it that day. Mostly a few 5-minute records of 25–40 mm, plus TA00052 (Chad) on 2015-03-05 and 2015-03-06, the two days after installation (about 6.2 mm every 5 minutes in the dry season). 154 station-days at 100 stations.
  - `2` **stuck_sensor**: 72 or more identical nonzero records in a row on a day with at least 144 wet records (steady phantom drizzle), or 6 or more identical records in a row of 5 mm or more. 34 station-days at 15 stations.
  - `4` **failing_station**: TA00416 (Kenya) from 2025-05-01, which reports hundreds of mm a month more than every neighbour.
  - Drop flagged values for extremes, maxima or threshold counts (`precip.where(precip_flag == 0)`); they move means very little (under 1% of all rainfall). Real storms such as Cyclone Idai (March 2019) and Storm Ana (January 2022) are confirmed by neighbouring stations and are not flagged.
- The older East and Southern Africa store `hindcasts/tahmo_precip.zarr` (the release's own grid, partial days kept) is left in the bucket but no longer read.
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
Writes the March-May 2024 gridded TAHMO rainfall over Kenya (60 cells with data).

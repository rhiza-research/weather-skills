---
name: tahmo-fetch
description: "Fetch daily TAHMO weather-station observations (rainfall, mean, max and min temperature, humidity, pressure) from the Trans-African Hydro-Meteorological Observatory network, for chosen station ids or every station in a lat/lon box (2015 to present), as a weather-skills station Zarr. Can also list the stations in a box, or read Sheerwater's historical QC'd daily TAHMO rainfall (2015-2025) from private GCS, per station with --gold-stations or as 0.25° cell means with --gridded. Needs TAHMO credentials (station mode) or Google Cloud credentials (--gold-stations, --gridded). Use when a task needs in-situ station data in Africa, for example to compare against gridded satellite or forecast data."
license: MIT
compatibility: Requires Python 3.12 and uv. Installs the TAHMO Python SDK from GitHub (git+https://github.com/rhiza-research/tahmo-api). Station mode needs TAHMO_API_USERNAME and TAHMO_API_PASSWORD; --gold-stations and --gridded need read access to gs://sheerwater-datalake/hindcasts via GCLOUD_SERVICE_CREDENTIALS (service-account JSON contents) or GOOGLE_APPLICATION_CREDENTIALS (path to that JSON).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.15"
  catalog-group: fetchers
  variables: "precip temperature tmax tmin humidity pressure"
  required-env: "TAHMO_API_USERNAME TAHMO_API_PASSWORD"
---

# tahmo-fetch

Fetches TAHMO station records as daily values in a station Zarr, or lists the stations in a box. `--gold-stations` instead reads historical all-Africa daily rainfall per station, and `--gridded` the 0.25° grid built from it.

## Usage

```bash
# List stations in a box (TSV on stdout)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox N/W/S/E
# Fetch chosen stations, or every TA station in a box
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py (--station ID [--station ID ...] | --bbox N/W/S/E) --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--day-start-hour H] [--workers N] -o OUT.zarr
# Historical station rainfall, no TAHMO login
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --gold-stations [--station ID ... | --bbox N/W/S/E] --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o OUT.zarr
# Historical gridded rainfall, no TAHMO login
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --gridded --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o OUT.zarr
# Print the latest day with a complete record, and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--station ID | --bbox N/W/S/E]
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest --gridded
```

### Date range
- Station mode: March 2015 to yesterday (`--probe-latest`). Station records vary and may have gaps; a fetch prints each station's complete days.
- `--gold-stations`, `--gridded`: 2015-03-05 to 2025-09-13, not updated.

### Arguments
- `--list-stations` — print the TA stations in `--bbox` (`station_id`, `name`, `latitude`, `longitude`, ISO `country`) and exit.
- `--station` — station id such as `TA00025`; repeatable. Overrides `--bbox`. An unknown id exits 2.
- `--bbox` — `N/W/S/E`. Without `--station`, selects every `TA` station in the box.
- `--start-time`, `--end-time` — inclusive days, `YYYY-MM-DD`.
- `--day-start-hour` — UTC hour each day starts at (default 0). With 6, day D runs 06 UTC D to 06 UTC D+1, as KMSA uses.
- `--workers` — concurrent station downloads (default 8). Lower it if TAHMO throttles.
- `--gold-stations` — read the gold station store (below). Takes `--station`, `--bbox` and the times; with neither, every station.
- `--gridded` — read the gridded store (below). Takes only the times and `--bbox`.
- `--probe-latest` — print the newest complete day in the last 14 days (default stations: the first three TA stations).
- `--output`, `-o` — output Zarr, overwritten.

### Output (station mode)
- Dims `(time, station_id)`, coords `latitude`, `longitude`, `name`, `country`; `featureType="timeSeries"`.
- `precip` (`mm day-1`, sum), `temperature`, `tmax`, `tmin` (`degree_Celsius`), `humidity` (`1`), `pressure` (`kPa`): whichever the stations report.
- `time` is every requested day, labelled by its start date; `data_interval` `1 day`.
- Each station starts after March 2015 and has gaps. Only records with TAHMO quality flag <= 2 are used.
- Stamps mark the end of each 5-minute interval, so day D holds D H:05 to D+1 H:00.
- A day is NaN unless all 288 records passed QC, for each variable separately (`tmax`/`tmin` follow `temperature`).

### Notes
- Station mode needs `TAHMO_API_USERNAME` and `TAHMO_API_PASSWORD` on every call; without them it exits 2.
- Never fill missing days. A multi-day total with any missing day is NaN; report how many stations were complete (stderr prints this).
- A failing station is dropped with a stderr line; no data at all exits 1.

### Gold station history (`--gold-stations`)
- Use it first for long historical station rainfall (seconds for all of Africa over 2015–2025); use station mode for recent days or other variables.
- Reads `gs://sheerwater-datalake/hindcasts/tahmo_africa_stations.zarr`: 587 stations, built from the TAHMO release `v0.0.0` gold files with the same complete-day rule as station mode (H = 0).
- Output: as station mode, with `precip` only, an `elevation` coord and a `precip_flag` coord (flags below). Stations with no complete day in the range are left out; missing days are NaN.
- An id with no gold record exits 2; no station in `--bbox` or no day in the range exits 1.

### Gridded history (`--gridded`)
- Use it for rainfall on a grid; it is `--gold-stations` averaged within each 0.25° cell.
- Reads `gs://sheerwater-datalake/hindcasts/tahmo_africa_precip.zarr`: 2015-03-05 to 2025-09-13, not updated. 475 cells from 587 stations, 17.5°W–48.5°E, 29.75°S–15.5°N.
- Output: `precip` (`mm day-1`) on `(time, latitude, longitude)`, a regular 0.25° grid cut to `--bbox`, with coords `precip_count` (stations averaged) and `precip_flag`. Cells with no station are NaN; never fill them.
- Each station goes to its nearest 0.25° cell; a cell-day is the unweighted mean of its stations.
- `precip_flag` (both stores) is a bit mask of suspect station-days, kept in `precip`. Filter with `precip.where(precip_flag == 0)` for extremes or threshold counts.
  - `1` unsupported_extreme: over 150 mm (or a 5-minute record over 25 mm) that neither CHIRPS nor a nearby station supports.
  - `2` stuck_sensor: the same nonzero value repeated for hours.
  - `4` failing_station: a station period far wetter than all its neighbours for months.
- Both need Google Cloud credentials: `GCLOUD_SERVICE_CREDENTIALS` (key JSON), else `GOOGLE_APPLICATION_CREDENTIALS` or ADC. No access, or no data in the box and range, exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 5.5/33.5/-5/42.5
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station TA00025 --station TA00026 --start-time 2026-09-01 --end-time 2026-09-30 -o tahmo_kenya.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --gold-stations --bbox 5.5/33.5/-5/42.5 --start-time 2024-03-01 --end-time 2024-05-31 -o tahmo_gold_kenya.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --gridded --start-time 2024-03-01 --end-time 2024-05-31 --bbox 5.5/33.5/-5/42.5 -o tahmo_grid_kenya.zarr
```
Lists Kenya's stations, fetches September 2026 for two of them, then March–May 2024 rainfall over Kenya per station and as a grid.

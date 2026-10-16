---
name: tahmo-fetch
description: "Fetch daily TAHMO weather-station observations (rainfall, temperature, humidity, pressure) from the Trans-African Hydro-Meteorological Observatory network, for chosen station ids or every station in a lat/lon box, as a weather-skills station Zarr. Can also list the stations in a box. Needs TAHMO credentials. Use when a task needs in-situ station data in Africa, for example to compare against gridded satellite or forecast data."
license: MIT
compatibility: Requires Python 3.12 and uv. Installs the TAHMO Python SDK directly from GitHub (git+https://github.com/rhiza-research/tahmo-api) via uv script metadata. Requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.3"
  catalog-group: fetchers
  variables:
    - precip
    - temperature
    - humidity
    - pressure
  openclaw:
    requires:
      env:
        - TAHMO_API_USERNAME
        - TAHMO_API_PASSWORD
    primaryEnv: TAHMO_API_USERNAME
---

# tahmo-fetch

Lists TAHMO stations in a box, or fetches their quality-controlled records and writes daily values as one station Zarr.

## Usage

```bash
# List stations in a box (TSV on stdout, no -o)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox N/W/S/E
# Fetch chosen stations, or every TA station in a box
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py (--station ID [--station ID ...] | --bbox N/W/S/E) --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--workers N] -o OUT.zarr
# Print the latest day with data, and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--station ID | --bbox N/W/S/E]
```

### Arguments
- `--list-stations` — print the TA stations in `--bbox` as TSV (`station_id`, `name`, `latitude`, `longitude`, `country` as ISO 3166-1 alpha-2) and exit. Requires `--bbox`.
- `--station` — station id such as `TA00025`; repeat for several. When given, `--bbox` is not used to select stations. An unknown id exits 2.
- `--bbox` — `N/W/S/E` decimal degrees. Without `--station`, selects every station whose id starts with `TA` inside the box. W > E crosses the antimeridian.
- `--start-time`, `--end-time` — inclusive date range, `YYYY-MM-DD`.
- `--workers` — concurrent station downloads. Default: 8. Lower it if TAHMO throttles.
- `--probe-latest` — print the latest day with data in the last 14 days for the `--station` / `--bbox` stations (default: the first three TA stations) and exit. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)` with coords `latitude`, `longitude`, `name`, `country` on `station_id`. `featureType="timeSeries"`, stamped `data_interval = "1 day"`.
- `precip` (`mm day-1`, daily sum), `temperature` (`degree_Celsius`), `humidity` (fraction, units `1`), `pressure` (`kPa`), each a daily mean except precip — whichever the stations report.
- Days are UTC days. Only records with TAHMO quality flag <= 2 are used; where several sensors report the same variable at the same time, the best-flagged one is kept.
- A day with no records is NaN, never 0 mm. Days with no data at any station are absent from `time`. Stations with no data are left out.

### Notes
- Set `TAHMO_API_USERNAME` and `TAHMO_API_PASSWORD` in the environment for every call, including `--list-stations` and `--probe-latest`; without them it exits 2.
- Stations often miss days. Never fill or extrapolate them. For multi-day totals, a station missing any day of the period must come out NaN; report how many stations were complete. stderr prints that count per variable (`precip: 144 of 153 stations complete`).
- A station whose fetch fails is dropped with a stderr line. No data for any station exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 5.5/33.5/-5/42.5
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station TA00025 --station TA00026 --start-time 2026-09-01 --end-time 2026-09-30 -o tahmo_kenya.zarr
```
Lists Kenya's TAHMO stations, then writes daily September 2026 values for two of them.

---
name: windborne-conditions-fetch
description: "Fetch WindBorne's plain-language forecast summaries (\"Partly cloudy, a breeze from the ENE…\") from the latest WeatherMesh-6 run at chosen lat/lon locations, per day, day/night half or hour, as a weather-skills Zarr of text, also printed to stderr. Needs WindBorne API credentials. Use when a briefing needs a readable outlook for a site."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Forecasts API (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - text
    - conditions
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-conditions-fetch

Fetches WindBorne's ranked, human-readable conditions for points from the latest WeatherMesh-6 run.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point LAT,LON[,ID] [--point ...] [--period daily|day_night|hourly] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--point` — `LAT,LON[,ID]`, repeatable; at least one. IDs default to `P0`, `P1`, …. Points outside the region the key may read return 403.
- `--period` — `daily` (default, 16 days), `day_night` (daytime and night halves split at local sunrise and sunset), or `hourly` (360 hours).
- `--probe-latest [IDENT]` — print the latest run's init date on stdout and exit; stderr lists every available run. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(station_id, time)`; `time` is each period's start (UTC) and `end_time` its end. `latitude`, `longitude` and the local `timezone` sit on `station_id`.
- `text` is the summary sentence; `conditions` the labels, most notable first, joined by `; `. A period one point lacks is an empty string.
- The same summaries are printed to stderr, one line per period.
- Stamps `windborne_initialization_time`, `windborne_forecast_zero` and `windborne_period`.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment; without them it exits 2. Never write their values into files or commands.
- Always the latest run. The text uses °F and mph, as WindBorne writes it; daily periods run between local midnights, given in UTC.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point -1.29,36.82,nairobi -o nairobi_conditions.zarr
```
Prints and writes 16 daily summaries for Nairobi.

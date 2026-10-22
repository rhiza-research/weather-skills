---
name: windborne-clim-fetch
description: "Fetch WindBorne's ERA5 1991–2020 hourly climatology (normals of 2 m temperature, dewpoint, precipitation and 10/100 m winds) at chosen lat/lon locations for a calendar date range, as a weather-skills station Zarr. Needs WindBorne API credentials. Use when a site forecast needs a climatological baseline to compare against."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Forecasts API (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - t2m
    - d2m
    - tp
    - u10
    - v10
    - ws10
    - u100
    - v100
    - ws100
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-clim-fetch

Fetches hourly ERA5 1991–2020 normals at points from the WindBorne climatology endpoint.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point LAT,LON[,ID] [--point ...] --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--rolling-days N] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--point` — `LAT,LON[,ID]`, repeatable; at least one. IDs default to `P0`, `P1`, …. Not limited to the forecast region.
- `--start-time`, `--end-time` — inclusive dates; the normals for every hour between them, laid on those real dates. Three months at once works.
- `--rolling-days` — smooth with a centred rolling mean over this many days. Default: 0 (raw hourly normals).
- `--probe-latest [IDENT]` — print `none` and exit: the normals are static, with no latest date. Needs no credentials. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(station_id, time)`, hourly from 00Z on `--start-time` to 23Z on `--end-time`, with `latitude` and `longitude` on `station_id`.
- `t2m`, `d2m` in `degree_Celsius`; winds in `m s-1`; these are valid at `time`. `tp` is a rate in `mm day-1` over `[time, time + 1 h)`.
- Mean sea-level pressure is not served, so it is left out.
- Stamps `data_interval = 1 hour`, `weather_skills_source=windborne-climatology` and `climatology_normal`.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment; without them it exits 2. Never write their values into files or commands.
- These are normals, not observed values.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point -1.29,36.82,nairobi --start-time 2026-10-01 --end-time 2026-12-31 -o nairobi_ond_normals.zarr
```
Writes hourly October–December normals for Nairobi.

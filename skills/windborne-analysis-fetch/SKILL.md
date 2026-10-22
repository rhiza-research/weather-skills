---
name: windborne-analysis-fetch
description: "Fetch the ECMWF IFS deterministic analysis (6-hourly, 0.25° global, about the last 25 days) as served by the WindBorne API, for a date range over an optional lat/lon box, as a weather-skills Zarr: 2 m temperature, dewpoint, pressure, winds, cloud, hourly precipitation and pressure-level fields. Needs WindBorne API credentials. Use as a recent ground truth for checking forecasts."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Forecasts API (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - t2m
    - d2m
    - msl
    - u10
    - v10
    - u100
    - v100
    - tcc
    - tp
    - t
    - z
    - q
    - u
    - v
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-analysis-fetch

Fetches ECMWF IFS deterministic analyses from WindBorne as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--start-time YYYY-MM-DD [--end-time YYYY-MM-DD]] [--bbox N/W/S/E] [-v VAR ...] [--level HPA ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive dates; every 00/06/12/18 UTC analysis between them. Default: the latest analysis only. About the last 25 days are served; a range outside them exits 1.
- `--bbox` — `N/W/S/E`. Default: global. Each analysis is downloaded global (~4 MB per field) and cut locally.
- `--variable`, `-v` — repeatable. Default: `t2m`. Surface: `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `u100`, `v100`, `tcc`. Pressure-level: `t`, `z`, `q`, `u`, `v`.
- `--level` — hPa, repeatable; required for pressure-level fields; with no pressure-level field it exits 2. 20 levels, 10–1000 hPa.
- `--probe-latest [IDENT]` — print the latest analysis date on stdout and exit; stderr gives the served range. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, plus `vertical` (hPa) for pressure-level fields. 0.25° grid.
- `t2m`, `d2m`, `t` in `degree_Celsius`; `msl` in `hPa`; `z` is geopotential in `m2 s-2`.
- `tp` is a rate in `mm day-1` over `[time, time + 1 h)`: the hour before each analysis, labelled at its start (05Z for the 06Z analysis). Stamped `data_interval = 1 hour`. The other five hours of each 6 h step are not served, so it does not add up to 6-hourly or daily totals.
- Other fields sit on the analysis times and stamp `data_interval = 6 hour` when there is more than one. With `tp` and other fields together, `time` holds both sets of times and each field is NaN at the other's. Stamps `weather_skills_source=windborne-ecmwf-analysis`.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment; without them it exits 2. Never write their values into files or commands.
- Prefer dynamical-fetch when the dynamical.org catalog carries the analysis you need: it is credential-free and keeps a longer history.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-10-18 --end-time 2026-10-19 --bbox 5.5/33.5/-5/42.5 -v t2m -v z --level 500 -o ecmwf_anl_kenya.zarr
```
Writes two days of 6-hourly 2 m temperature and 500 hPa geopotential analyses over Kenya.

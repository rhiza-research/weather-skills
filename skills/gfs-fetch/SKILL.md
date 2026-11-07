---
name: gfs-fetch
description: "Fetch a NOAA GFS forecast, GEFS 31-member ensemble forecast (to 35 days; GEFSv12 reforecast for 1998-2019 inits), or GFS / GEFS analysis (0.25° global) for an init date or date range over a region from the public dynamical.org catalog, as a weather-skills Zarr. No credentials needed. Use when a task needs GFS or GEFS precipitation, temperature, winds, pressure or other surface fields."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public data from the dynamical.org catalog and, for GEFS inits before 2020-10-01, the public GEFSv12 reforecast; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - temperature_2m
    - geopotential_height
---

# gfs-fetch

Fetches NOAA GFS or GEFS from the [dynamical.org catalog](https://dynamical.org/catalog/), subset by bbox, time, and variables, as a weather-skills standard dataset Zarr.

| `--product` | Members | Leads / time step | Coverage |
|---|---|---|---|
| `gefs` (default) | 31 (0 = control) | 3-hourly to 10 days, then 6-hourly to 35 days | 0.25° global, 00 UTC inits from 2020-10-01 |
| `gefs`, inits before 2020-10-01 (GEFSv12 reforecast) | 11 (0 = control) | daily, steps 0–34 days | 1° global, Wednesday inits 1998-12-30 to 2019-08-28; `precip` and `temperature_2m` only |
| `gfs` | 1 | hourly to 5 days, then 3-hourly to 16 days | 0.25° global, inits from 2021-05 |
| `gfs-analysis` | — | hourly | 0.25° global, from 2021-05 |
| `gefs-analysis` | — | 3-hourly | 0.25° global, from 2000 |

## Usage

```bash
# Forecast: one 00 UTC init
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product gefs|gfs] --date YYYY-MM-DD [--bbox N/W/S/E] [--max-lead 240h] -v VAR [-v VAR ...] -o OUT.zarr
# Forecast: every 00 UTC init in a window, in one call
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product gefs|gfs] --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [--max-lead 240h] -v VAR -o OUT.zarr
# Analysis: inclusive date range
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product gfs-analysis|gefs-analysis --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -v VAR -o OUT.zarr
# Several windows: repeat the pair, e.g. each January
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2024-01-01 --end-time 2024-01-31 --start-time 2025-01-01 --end-time 2025-01-31 -v VAR -o OUT.zarr
# Latest init or analysis day (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product P] --probe-latest
# A product's variables and units (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product P] --list-variables
```

### Arguments
- `--product` — `gefs` (default), `gfs`, `gfs-analysis` or `gefs-analysis`; see the table above.
- `--date` — forecast init date; selects the 00 UTC init.
- `--start-time`, `--end-time` — a window; `--end-time` includes the whole end day (hourly GFS analysis for one day is 24 steps). Repeat the pair for several windows, one window per pair; not with `--date`. Analyses keep the times in the windows. Forecasts take every 00 UTC init in them with all their leads: like `--date`, the windows select init times, not valid times. Use this instead of one `--date` call per init. A range of inits ending today warns when the newest init is still publishing; its missing leads stay NaN.
- `--max-lead` — forecasts only: keep output steps up to this one (`240h`, `10D`). Default: every step.
- `--bbox` — `N/W/S/E`, longitudes −180..180; W > E crosses the antimeridian. Omit for the full grid.
- `--variable`, `-v` — catalog variable name; repeat for several. Aliases: `precip`/`tp`/`pr` → `precipitation_surface`; `t2m` → `temperature_2m`; `msl` → `pressure_reduced_to_mean_sea_level`; `sp` → `pressure_surface`; `u10`/`v10`/`u100`/`v100` → `wind_u_10m`/`wind_v_10m`/`wind_u_100m`/`wind_v_100m`; `gh` → GEFS 500 hPa geopotential height. `NAME` also matches `NAME_surface`. An unknown name exits and lists `Available:`. Omitting `-v` fetches every field.
- `--probe-latest` — print the product's latest init or analysis date and exit. Takes no value. stderr notes a forecast init whose last lead is not published yet; an analysis's latest day is partial (stderr says so).
- `--list-variables` — print each of the product's variables (name, units, long name), then exit. Does not download fields.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Forecasts: `(step, [number,] latitude, longitude)` with scalar `time` = init; with windows, `(time, step, [number,] latitude, longitude)`, one `time` per init. Analyses: `(time, latitude, longitude)`. GEFS `geopotential_height` adds `vertical` (500 hPa).
- Rainfall is written as `precip` in `mm day-1`; other fields keep their catalog names. Air temperature in `degree_Celsius`, other units as published. Longitude is `[-180, 180)`, latitude ascending.
- `weather_skills_source` names the source used.
- Requested windows or leads past what is published are written as available, with a `WARNING` on stderr; nothing is padded. No data at all exits 1.

### Historical GEFS inits
`--product gefs` inits before 2020-10-01 come from the GEFSv12 reforecast; stderr names the source used.
- The reforecast has Wednesday inits only, 1998-12-30 to 2019-08-28; other days and 2019-08-29 to 2020-09-30 have none. No init at all in the request exits 1.
- It offers `precip` (`precip`/`tp`/`pr`) and `temperature_2m` (`t2m`) only, in `mm day-1` and `degree_Celsius`; other `-v` names exit 2. Omitting `-v` fetches both.
- Steps are daily: `step = d` is the day `[init + d, init + d + 1)`, 0 to 34 days. Members are 0–10 (0 = control). Dims and names are as for live GEFS; `--bbox` and `--max-lead` apply; `--probe-latest` and `--list-variables` describe live GEFS only.
- A variable with no run for an init stays NaN for it, with a `WARNING`; nothing is filled.
- A request with inits on both sides of 2020-10-01 exits 2: split it there into two calls (member count and step spacing differ).

### Notes
- **Forecast `step` is the interval start.** dynamical.org labels averaged fields (precip, radiation, cloud cover) at the end of their interval, so lead 0 is NaN. When every selected field is NaN at lead 0, fetch drops that lead and shifts each step back one, so `step = 0` is the first interval and daily or weekly bins line up with `aggregate-temporal`. Where lead spacing changes (GFS 1- to 3-hourly, GEFS 3- to 6-hourly), fetch writes `step_bounds`. Instant fields (`temperature_2m`, winds, pressure) keep their own labels; on uneven leads each sample's cell is `[s, next lead)`. Fetched together with averaged fields, the cells end at each step instead, so fetch averaged and instant fields separately when you need interval-start steps.
- GFS publishes 00/06/12/18 UTC inits; only the 00 UTC init of each day is fetched.
- GEFS reads every member: pass `--bbox`, `-v` and `--max-lead` to keep reads small.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product gefs --date 2026-10-12 \
  --bbox 5.5/33.5/-5/42.5 -v precip --max-lead 14D -o gefs_kenya.zarr
```
Writes 31-member 3- then 6-hourly precip rates for 14 days over Kenya.

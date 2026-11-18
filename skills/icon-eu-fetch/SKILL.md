---
name: icon-eu-fetch
description: "Fetch a DWD ICON-EU deterministic forecast (~7 km over Europe, hourly leads to 5 days) for an init date or date range over a region from the public dynamical.org catalog, as a weather-skills Zarr. No credentials needed. Use when a task needs a high-resolution European short-range forecast of precipitation, temperature, dewpoint, wind, pressure, cloud, snow or CAPE."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public data from the dynamical.org catalog; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - temperature_2m
    - dew_point_temperature_2m
---

# icon-eu-fetch

Fetches the DWD ICON-EU forecast from the [dynamical.org catalog](https://dynamical.org/catalog/), subset by bbox, init, lead, and variables, as a weather-skills standard dataset Zarr.

| Members | Leads | Coverage |
|---|---|---|
| 1 | hourly to 78 h, then 3-hourly to 5 days | 0.0625° (~7 km), 29.5–70.5°N, 23.5°W–62.5°E; 00 UTC inits from 2026-02 |

## Usage

```bash
# One 00 UTC init
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD [--bbox N/W/S/E] [--max-lead 48h] -v VAR [-v VAR ...] -o OUT.zarr
# Every 00 UTC init in a window, in one call
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [--max-lead 48h] -v VAR -o OUT.zarr
# Latest init (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
# Variables and units (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-variables
```

### Arguments
- `--date` — init date; selects the 00 UTC init.
- `--start-time`, `--end-time` — a window of init dates, `--end-time` inclusive. Repeat the pair for several windows; not with `--date`. Every 00 UTC init in them is taken with all its leads: the windows select init times, not valid times. A range of inits ending today warns when the newest init is still publishing; its missing leads stay NaN.
- `--max-lead` — keep output steps up to this one (`48h`, `3D`). Default: every step.
- `--bbox` — `N/W/S/E`, longitudes −180..180. Omit for the full European grid.
- `--variable`, `-v` — catalog variable names, several per flag, repeated, or comma-separated (`-v precip t2m`). Aliases: `precip`/`tp`/`pr` → `precipitation_surface`; `t2m` → `temperature_2m`; `d2m` → `dew_point_temperature_2m`; `msl` → `pressure_reduced_to_mean_sea_level`; `sp` → `pressure_surface`; `u10`/`v10` → `wind_u_10m`/`wind_v_10m`. `NAME` also matches `NAME_surface`. An unknown name exits and lists `Available:`. Omitting `-v` fetches every field.
- `--probe-latest` — print the latest init date and exit. Takes no value. stderr notes an init whose last lead is not published yet.
- `--list-variables` — print each variable (name, units, long name), then exit. Does not download fields.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `(step, latitude, longitude)` with scalar `time` = init; with windows, `(time, step, latitude, longitude)`, one `time` per init.
- Rainfall is written as `precip` in `mm day-1`; other fields keep their catalog names. Air and dewpoint temperature in `degree_Celsius`, other units as published. Latitude ascending.
- Leads past what is published are written as available, with a `WARNING` on stderr; nothing is padded. No data at all exits 1.

### Notes
- **`step` is the interval start.** dynamical.org labels averaged fields (precip, radiation) at the end of their interval, so lead 0 is NaN. When every selected field is NaN at lead 0, fetch drops that lead and shifts each step back one, so `step = 0` is the first hour; `step_bounds` records the 1- to 3-hourly cells. Instant fields (`temperature_2m`, winds, pressure) keep their own labels, each sample's cell running `[s, next lead)`. Fetched together with averaged fields, the cells end at each step instead, so fetch averaged and instant fields separately when you need interval-start steps.
- Only the 00 UTC init of each day is fetched.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-12 \
  --bbox 55/-11/49/2 -v precip -v t2m -o icon_eu_uk.zarr
```
Writes ICON-EU precip and 2 m temperature for 5 days over the British Isles. Because `t2m` is finite at lead 0, steps keep their published labels; fetch `precip` alone for interval-start steps.

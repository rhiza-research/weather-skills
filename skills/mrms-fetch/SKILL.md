---
name: mrms-fetch
description: "Fetch NOAA MRMS hourly radar-and-gauge precipitation analysis (0.01°, ~1 km, contiguous US, 2014 to a few hours ago) for a date range over a region from the public dynamical.org catalog, as a weather-skills Zarr. No credentials needed. Use when a task needs observed hourly rainfall over the United States."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public data from the dynamical.org catalog; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
---

# mrms-fetch

Fetches the NOAA Multi-Radar Multi-Sensor (MRMS) hourly precipitation analysis from the [dynamical.org catalog](https://dynamical.org/catalog/), subset by bbox, time, and variables, as a weather-skills standard dataset Zarr.

| Time step | Coverage |
|---|---|
| hourly | 0.01° (~1 km), 20–55°N, 130–60°W, from 2014-11 |

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [-v VAR ...] -o OUT.zarr
# Several windows: repeat the pair, e.g. each July
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2024-07-01 --end-time 2024-07-31 --start-time 2025-07-01 --end-time 2025-07-31 --bbox N/W/S/E -v precip -o OUT.zarr
# Latest day (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
# Variables and units (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-variables
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range; `--end-time` includes the whole end day (24 hourly steps). Repeat the pair for several windows, one window per pair.
- `--bbox` — `N/W/S/E`, longitudes −180..180. Omit for the full CONUS grid (about 25 million cells per hour; always pass a bbox for more than a few hours).
- `--variable`, `-v` — catalog variable names, several per flag, repeated, or comma-separated (`-v precip t2m`). Aliases: `precip`/`tp`/`pr` → `precipitation_surface`. `NAME` also matches `NAME_surface` (`precipitation_radar_only`, `precipitation_pass_1`). An unknown name exits and lists `Available:`. Omitting `-v` fetches every field.
- `--probe-latest` — print the latest day and exit. Takes no value. stderr notes that the latest day is partial.
- `--list-variables` — print each variable (name, units, long name), then exit. Does not download fields.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `(time, latitude, longitude)`, latitude ascending, `data_interval` `1 hour`.
- Rainfall (`precipitation_surface`) is written as `precip` in `mm day-1`; the other fields (`precipitation_pass_1_surface`, `precipitation_pass_2_surface`, `precipitation_radar_only_surface`, `categorical_precipitation_type_surface`, `flash_qpe_ffg_max_surface`) keep their catalog names, with precip rates also in `mm day-1`.
- A window past the latest published hour is written as available, with a `WARNING` on stderr; nothing is padded. No data in any window exits 1.

### Notes
- Each `time` labels the hour by its published timestamp. For daily totals, run `aggregate-temporal --period daily` and then `convert-to-totals`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-09-01 --end-time 2026-09-07 \
  --bbox 31/-99/29/-97 -v precip -o mrms_austin.zarr
```
Writes a week of hourly MRMS rainfall rates around Austin, Texas.

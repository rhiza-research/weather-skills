---
name: arco-era5-fetch
description: "Fetch ERA5 hourly reanalysis (temperature, wind, precipitation, pressure-level fields, and more) for a date range and region from Google's public ARCO-ERA5 Zarr store, as a weather-skills Zarr. No credentials needed. Use when a task needs gridded reanalysis as ground truth for comparison, verification, or analysis."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public ARCO-ERA5 analysis-ready Zarr from Google Cloud (gs://gcp-public-data-arco-era5) over anonymous access; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - 2m_temperature
    - total_precipitation
    - 2m_dewpoint_temperature
    - 10m_u_component_of_wind
    - 10m_v_component_of_wind
---

# arco-era5-fetch

Fetches hourly 0.25° ERA5 fields from the [ARCO-ERA5](https://github.com/google-research/arco-era5) store, subset by bbox, dates, and variables, as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -v VAR [-v VAR ...] -o OUT.zarr
# Last day with data (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range; the end day runs to 23 UTC.
- `--bbox` — `N/W/S/E` decimal degrees, longitudes −180..180; W > E crosses the antimeridian. Omit for the full globe.
- `--variable`, `-v` — ARCO variable name (`2m_temperature`, `total_precipitation`, `10m_u_component_of_wind`, pressure-level `temperature`, ...); repeat for several. An unknown name exits 2 and lists `Available:`. Omitting `-v` fetches all ~270 fields.
- `--probe-latest` — print the last day with data (`YYYY-MM-DD`) and exit. Takes an optional id that this skill ignores.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, hourly, plus `level` (hPa) for pressure-level fields. Longitude is `[-180, 180)`.
- `2m_temperature` and `temperature` in `degree_Celsius`. Precip amounts (`total_precipitation`, `convective_precipitation`, `large_scale_precipitation`) become `mm day-1` rates, labelled at the start of their hour, so a day's 00–23 UTC rows cover that day. Other fields keep ERA5 units and labels.
- Every variable has `data_interval="1 hour"`; the dataset has `weather_skills_source="arco-era5"`.

### Notes
- Data lags real time by about 6 days. End the window at `--probe-latest`; hours past the data end are dropped with a stderr warning, and a window with no data exits 1. Days after the store's final-ERA5 date (about 3 months back) are preliminary ERA5T.
- The latest day's last hour of precip is NaN until the next hour is published.
- Each hour of each variable downloads the whole globe; `--bbox` only trims what is kept. Pass only the variables you need and keep windows to days or weeks. A 37-level field costs about 3.7 GB of download per day.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-10-01 --end-time 2026-10-07 \
  --bbox 5.5/33.5/-5/42.5 -v 2m_temperature -v total_precipitation -o era5_kenya.zarr
```
Writes one week of hourly 2 m temperature (°C) and precip rates (mm day-1) over Kenya.

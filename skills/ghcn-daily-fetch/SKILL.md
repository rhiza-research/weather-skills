---
name: ghcn-daily-fetch
description: "Fetch NOAA GHCN-Daily station observations (daily precipitation and max, min, mean temperature) for a date range and lat/lon box, as a weather-skills station Zarr. No credentials needed; covers about 130,000 stations worldwide. Use when a task needs daily in-situ station data anywhere in the world, for example to compare against gridded satellite, reanalysis, or forecast data."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads NOAA GHCN-Daily from NCEI's public HTTPS tree (www.ncei.noaa.gov/pub/data/ghcn/daily); no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.5"
  catalog-group: fetchers
  variables:
    - precip
    - tmax
    - tmin
    - tavg
---

# ghcn-daily-fetch

Downloads GHCN-Daily station records from NCEI's public HTTPS tree and writes the stations inside a box as one station Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [-v VAR ...] [--workers N] -o OUT.zarr
# Print the newest observation date and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range, `YYYY-MM-DD`.
- `--bbox` — `N/W/S/E` decimal degrees; selects stations by their listed location. W > E crosses the antimeridian. Omit for every station worldwide.
- `--variable`, `-v` — `precip`, `tmax`, `tmin`, or `tavg`; repeat for several. Default: `precip tmax tmin`.
- `--workers` — concurrent station downloads. Default: 8. Lower it if the server throttles.
- `--probe-latest` — print the newest observation date in the current year's GHCN-Daily file and exit. Takes no value; streams a ~100 MB file, so it takes tens of seconds. Some stations lag that date by days.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)` with coords `latitude`, `longitude`, `name` on `station_id`. A CF-1.13 timeSeries station dataset (`featureType="timeSeries"`).
- `precip` in `mm day-1`; `tmax`, `tmin`, `tavg` in `degree_Celsius`. Stamped `data_interval = "1 day"`.
- Only quality-passed values are kept. Precip zeros that GHCN inferred rather than measured (measurement flag `P` or `I`) are dropped; trace precip is 0.
- `time` is each station's local observation day, not a UTC day. The axis runs daily from `--start-time` to the last day any station reported; days nobody reported are NaN. Nothing is padded past the data.
- Stations with no kept value are left out. A requested variable no station reported is left out with a warning.

### Notes
- Each station is a separate whole-history download, so keep `--bbox` tight. A station that fails twice is dropped; stderr reports the count.
- No stations or no data in the window exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 --start-time 2026-09-01 --end-time 2026-09-30 -v precip -o ghcn_kenya.zarr
```
Writes daily precip for every Kenyan GHCN station that reported in September 2026.

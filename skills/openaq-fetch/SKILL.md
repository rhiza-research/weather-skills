---
name: openaq-fetch
description: "Fetch OpenAQ air-quality station observations (daily mean PM2.5, PM10, NO2, O3, SO2, CO) for a date range and lat/lon box, as a weather-skills station Zarr. Needs a free OPENAQ_API_KEY. Use when a task needs in-situ air-quality or atmospheric-composition data, for example to compare against gridded model output."
license: MIT
compatibility: Requires Python 3.12 and uv. Uses the OpenAQ v3 REST API over HTTPS; requires a free OPENAQ_API_KEY in the environment (register at https://explore.openaq.org/register).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - pm25
    - pm10
    - no2
    - o3
    - so2
    - co
  openclaw:
    requires:
      env:
        - OPENAQ_API_KEY
    primaryEnv: OPENAQ_API_KEY
    envVars:
      - name: OPENAQ_API_KEY
        description: Free personal token from https://explore.openaq.org/register
---

# openaq-fetch

Downloads daily air-quality values for every OpenAQ monitoring location inside a box and writes them as one station Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox N/W/S/E --start-time YYYY-MM-DD --end-time YYYY-MM-DD [-v VAR ...] [--min-coverage F] [--workers N] -o OUT.zarr
# Print the latest UTC day any PM2.5 sensor reported, and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--bbox` — `N/W/S/E` decimal degrees. Required. Must not cross the antimeridian (W <= E); split such a region into two calls.
- `--start-time`, `--end-time` — inclusive range of station-local days, `YYYY-MM-DD`.
- `--variable`, `-v` — `pm25`, `pm10`, `no2`, `o3`, `so2`, or `co`; repeat for several. Default: all six.
- `--min-coverage` — fraction (0–1) of a day's 24 hourly values needed to keep its daily mean. Default: 0.75. Days below it are NaN.
- `--workers` — concurrent sensor downloads. Default: 8. Requests stay under OpenAQ's limits (60/minute, 2,000/hour) whatever the value.
- `--probe-latest` — print the latest UTC date (within the last 7 days) any PM2.5 sensor reported, and exit. That day is usually still partial. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)` with coords `latitude`, `longitude`, `name` on `station_id`; `station_id` is the OpenAQ location id. A CF-1.13 timeSeries station dataset.
- Each value is the mean of the hourly values over the station's local calendar day, so days are not aligned across time zones. `time` is that local date. Several sensors for one pollutant at a location are averaged.
- Units are as OpenAQ reports them (µg/m³ for particulates, ppm or ppb for gases), not converted. Each pollutant uses the first unit seen; sensors in another unit, or with no unit, are dropped.
- Negative daily means and days below `--min-coverage` are NaN; stderr counts each. Days no station kept are absent from `time`.
- A requested pollutant with no data is left out with a warning.

### Notes
- Set `OPENAQ_API_KEY` in the environment on the first call, including `--probe-latest`. A missing key exits 2; a rejected key exits 1. The key is never printed.
- A sensor that fails twice is dropped; stderr reports the count. No sensors or no data exits 1.
- Every sensor is a separate rate-limited request (about 32 per minute), so a large box or long window is slow.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox -1.1/36.6/-1.45/37.1 --start-time 2026-09-01 --end-time 2026-09-30 -v pm25 -o openaq_nairobi.zarr
```
Writes daily mean PM2.5 for each Nairobi monitoring location in September 2026.

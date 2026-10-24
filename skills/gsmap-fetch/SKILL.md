---
name: gsmap-fetch
description: "Fetch JAXA GSMaP v6 gauge-corrected hourly 0.1° precipitation (archive from April 2025, 60°N-60°S) for a region and date range from JAXA's public Earth API, as a weather-skills Zarr. No credentials needed. Use when a task needs recent hourly satellite rainfall, or an independent satellite estimate to cross-check other precipitation products."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads JAXA's public Earth API (data.earth.jaxa.jp); no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.3"
  catalog-group: fetchers
  variables:
    - precip
---

# gsmap-fetch

Reads hourly GSMaP (Global Satellite Mapping of Precipitation) images for a box from JAXA's Earth API, which subsets on the server, and writes them as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD --bbox N/W/S/E -o OUT.zarr
```

### Arguments
- `--start-time`, `--end-time` — inclusive range of whole UTC days.
- `--bbox` — required `N/W/S/E` decimal degrees. Latitudes must lie within 60°N–60°S, else exit 2. Longitudes may be −180..180 or 0..360; a box crossing the antimeridian (W > E) exits 2, so fetch each side separately.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, hourly, 0.1° cell centres. Latitude is descending; longitude is `[-180, 180)`.
- `precip` in `mm day-1` (converted from `mm/hr`), `data_interval="1 hour"`. Each `time` labels the start of its hour (`02:00` covers 02:00–02:59 UTC). JAXA error codes and nodata are NaN.
- `weather_skills_source="jaxa-earth-api:JAXA.EORC_GSMaP_standard.Gauge.v6_hourly"`.

### Notes
- JAXA serves no 00 or 01 UTC image on any day, so each day has at most 22 rows. Missing hours are absent rows, not filled, and stderr reports how many hours were served. Daily means therefore cover at most 22 of 24 hours, and complete daily totals are not available.
- Days JAXA has not published (before April 2025, or the last few days) are skipped with a stderr note. A range with no published day exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-10-05 --end-time 2026-10-11 \
  --bbox 5.5/33.5/-5/42.5 -o gsmap_kenya.zarr
```
Writes up to 154 hourly precip-rate maps (mm day-1) over Kenya for one week.

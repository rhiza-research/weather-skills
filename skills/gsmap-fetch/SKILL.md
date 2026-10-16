---
name: gsmap-fetch
description: Fetch JAXA GSMaP v6 gauge-corrected hourly precipitation for a `--bbox` region from JAXA's credential-free Earth API and write a weather-skills standard dataset Zarr. 0.1 degree, hourly, archive from April 2025. JAXA serves no 00 or 01 UTC images, so every UTC day has 22 hours and daily totals are never complete. Use when a task needs recent hourly precipitation observations; not for a climatology (use clim-fetch) or for CHIRPS/IMERG (use chirps-fetch / dynamical-fetch).
license: MIT
compatibility: Requires Python 3.12 and uv. Reads JAXA's public Earth API (data.earth.jaxa.jp); no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
---

# gsmap-fetch

Reads JAXA's GSMaP (Global Satellite Mapping of Precipitation) v6
gauge-corrected hourly rate (`JAXA.EORC_GSMaP_standard.Gauge.v6_hourly`)
through JAXA's own Earth API (`jaxa-earth`, Cloud-Optimized GeoTIFF + STAC),
for a bounded region and date range, and writes a standard-dataset Zarr with
one `precip` variable at native hourly cadence. No credentials or
registration; the bbox is subset server-side. JAXA publishes each day about
three days late.

## When to use

- Recent precipitation observations at hourly resolution for a specific
  region.
- As a cross-check against CHIRPS/IMERG, with different input sensors,
  retrieval algorithm, and gauge-adjustment approach.

Not for a climatology (`clim-fetch`), and not for history before April 2025
(the JAXA archive starts there, although its catalog metadata says 2000).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time YYYY-MM-DD --end-time YYYY-MM-DD --bbox N/W/S/E -o <path.zarr>
```

### Arguments

- `--start-time`, `--end-time` — inclusive calendar date range (whole UTC
  days). A day JAXA has not published is skipped with a stderr note; a range
  with no published day exits 1.
- `--bbox` — required `N/W/S/E` bounding box; there is no whole-globe fetch.
  Latitudes must lie within 60N–60S (GSMaP's coverage), else exit 2.
  Longitudes may be −180..180 or 0..360 (output is always −180..180); an
  antimeridian-crossing box (west > east) exits 2 — fetch each side and
  `concat`. Use `resolve-region` to turn a country/named region into this value.
- `--output`, `-o` — output Zarr path.

### Output

Zarr with dims `(time, latitude, longitude)` at 0.1° resolution (cell
centres on x.x5°, latitude descending) and data variable `precip` (`mm day-1`,
converted from the source's `mm/hr`). JAXA's error codes and nodata (negative
values) are NaN. Stamped `data_interval` `1 hour`; each `time` labels the start
of its hour (JAXA's `start_datetime`), e.g. `02:00` covers 02:00–02:59 UTC.

`time` holds one row per hour JAXA actually serves, labeled from the archive's
own per-image hour; missing hours are absent rows, not NaN-filled. JAXA serves
no 00 or 01 UTC image on any day, so each UTC day has at most 22 rows;
stderr reports how many of the requested hours were served. Run
`aggregate-temporal --period daily` for daily mean rates: each day carries
`aggregation_coverage` ≤ 22/24, so `convert-to-totals` (default
`--min-coverage 1.0`) drops every day — a complete daily total is not
available from this source.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

```bash
# Kenya, one week, hourly.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time 2026-09-15 --end-time 2026-09-21 \
  --bbox 5.506/33.894/-4.677/41.855 -o /tmp/gsmap_kenya.zarr

# Then daily mean rates (22 of 24 hours per day):
uv run ${CLAUDE_SKILL_DIR}/../aggregate-temporal/scripts/aggregate.py \
  -i /tmp/gsmap_kenya.zarr -o /tmp/gsmap_kenya_daily_rate.zarr --period daily
```

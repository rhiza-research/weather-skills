---
name: gsmap-fetch
description: Fetch JAXA GSMaP hourly precipitation (gauge-corrected, satellite-only fallback) for a `--bbox` region from Google Earth Engine and write a weather-skills standard dataset Zarr. 0.1 degree resolution, native hourly cadence -- run aggregate-temporal --period daily before feeding daily-oriented skills. Requires Earth Engine credentials (service account, or a locally-authenticated personal login). Use when a task needs live/near-real-time precipitation observations at hourly resolution; not for a climatology (use clim-fetch) or for CHIRPS/IMERG (use chirps-fetch/imerg-fetch).
license: MIT
compatibility: Requires Python 3.12 and uv. Requires Earth Engine access -- see "Authentication" below.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
---

# gsmap-fetch

Reads JAXA's GSMaP (Global Satellite Mapping of Precipitation) hourly rate
from Earth Engine (`JAXA/GPM_L3/GSMaP/v6/operational` -- **not** `v7`, see
"Dataset version" below), for a bounded region and time range, and writes a
standard-dataset Zarr with one `precip` variable at native hourly cadence.

## When to use

- Live/near-real-time precipitation observations at hourly resolution for a
  specific region (Earth Engine needs a bounded query; there is no
  whole-globe fetch here).
- As an alternative or cross-check against CHIRPS/IMERG, with different
  input sensors, retrieval algorithm, and gauge-adjustment approach.

Not for a climatology (`clim-fetch`), and not a drop-in replacement for
`chirps-fetch`/`imerg-fetch` — those read plain public HTTPS/GCS with no
auth; this one requires Earth Engine credentials (see below).

## Dataset version

Use `v6/operational`, not `v7/operational`. Despite the version number, `v7`
is a much more lagged pipeline (confirmed directly: its real latest data was
~3 months stale when checked, vs. `v6`'s which was current to within a day).
Don't trust either collection's catalog-page-stated date range at face
value — check the real coverage yourself:

```python
import ee

ee.Initialize()
coll = ee.ImageCollection("JAXA/GPM_L3/GSMaP/v6/operational")
print(coll.aggregate_min("system:time_start").getInfo())
print(coll.aggregate_max("system:time_start").getInfo())  # ms since epoch
```

## Authentication

Every Earth Engine call needs credentials — there is no anonymous public
HTTPS path the way `chirps-fetch`/`imerg-fetch` have. Two modes:

- **Headless (recommended for an agent-run skill)**: set both
  `GEE_SERVICE_ACCOUNT_KEY` (path to a service-account JSON key) and
  `GEE_SERVICE_ACCOUNT_EMAIL` (that service account's email). Optionally
  `GEE_PROJECT` to pick the initializing project explicitly. See Earth
  Engine's own [service account guide](https://developers.google.com/earth-engine/guides/service_account)
  for how to create one — the project it belongs to must itself be
  registered for Earth Engine (free for noncommercial use).
- **Interactive (this machine only)**: if neither env var is set, falls
  back to plain `ee.Initialize()`, reusing whatever Application Default
  Credentials or `earthengine authenticate` session already exists locally.
  Not portable — this only works on a machine where you've personally
  logged in.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time YYYY-MM-DD --end-time YYYY-MM-DD --bbox N/W/S/E -o <path.zarr>
```

### Arguments

- `--start-time`, `--end-time` — inclusive calendar date range (whole UTC
  days). Output covers every hour in that range.
- `--bbox` — required `N/W/S/E` bounding box. Earth Engine needs a bounded
  region to build a grid; unlike `chirps-fetch` there's no whole-globe
  default. Use `resolve-region` to turn a country/named region into this
  value.
- `--output`, `-o` — output Zarr path.

### Output

Zarr with dims `(time, latitude, longitude)` at 0.1° resolution, one hourly
row per UTC hour in range. `precip` (`mm day-1`, converted from the source's
native `mm/hr`) is the gauge-corrected rate (`hourlyPrecipRateGC`), falling
back to the raw satellite-only rate (`hourlyPrecipRate`) wherever the
gauge-corrected value is null — typically the most recent few hours, before
gauge data has caught up. Stamped `data_interval` `1 hour`. Run
`aggregate-temporal --period daily` (then `convert-to-totals` for `mm`
totals) before feeding a daily-oriented skill.

### Provenance

Standard `weather_skills_history` stamp via the decorator, plus
`weather_skills_source=earth-engine:JAXA/GPM_L3/GSMaP/v6/operational`.

## Example

```bash
# Kenya, one week, hourly.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time 2026-09-15 --end-time 2026-09-21 \
  --bbox 5.506/33.894/-4.677/41.855 -o /tmp/gsmap_kenya.zarr

# Then, for a daily accumulation map:
uv run ${CLAUDE_SKILL_DIR}/../aggregate-temporal/scripts/aggregate.py \
  -i /tmp/gsmap_kenya.zarr -o /tmp/gsmap_kenya_daily_rate.zarr --period daily
uv run ${CLAUDE_SKILL_DIR}/../convert-to-totals/scripts/convert_to_totals.py \
  -i /tmp/gsmap_kenya_daily_rate.zarr -o /tmp/gsmap_kenya_daily_total.zarr
```

---
name: smap-fetch
description: "Fetch NASA SMAP Enhanced L3 (SPL3SMP_E) daily 9 km volumetric surface soil moisture for a region and short date range via NASA Earthdata, as a weather-skills Zarr. Needs an Earthdata login. Use when a task needs gridded soil-moisture observations, e.g. for drought or agricultural analysis, or comparison against models."
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`. Earthdata accounts are free with sign-up at https://urs.earthdata.nasa.gov/users/new.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - soil_moisture
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# smap-fetch

Downloads one global SMAP granule (data file) per day from NASA Earthdata, keeps the requested box and overpass, and writes a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD --bbox N/W/S/E [--overpass AM|PM] [--quality recommended|all] -o OUT.zarr
# Latest published day (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

Needs `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` (or `EARTHDATA_TOKEN`) in the environment, or a `urs.earthdata.nasa.gov` entry in `~/.netrc`. Failed login exits 1 with that hint; it never prompts.

### Arguments
- `--start-time`, `--end-time` — inclusive date range.
- `--bbox` — `N/W/S/E` decimal degrees, longitudes −180..180; W > E crosses the antimeridian. Optional, but omit it only when you need the whole globe.
- `--overpass` — `AM` (6 am local, descending orbit) or `PM` (6 pm local, ascending). One per run; fetch each separately for both. Default: `AM`.
- `--quality` — `recommended` keeps only retrievals flagged recommended quality; `all` also keeps lower-quality ones (e.g. dense vegetation), often more coverage. Default: `recommended`.
- `--probe-latest` — print the latest granule day in the last 21 days (`YYYY-MM-DD`) and exit; needs a login. Takes an optional id that this skill ignores.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, daily, on the 9 km EASE-Grid 2.0 cell centres. Latitude is descending and unevenly spaced in degrees (about 0.07° near the equator); longitude is evenly spaced.
- `soil_moisture` in `m3 m-3` (volume of water per volume of soil), `standard_name=volume_fraction_of_condensed_water_in_soil`, `data_interval="1 day"`. NaN marks ocean, no retrieval, values outside 0.02–0.5, and (with `--quality recommended`) lower-quality retrievals.
- A `latitude_longitude` grid-mapping variable (WGS84); `weather_skills_source="smap"`.

### Notes
- Each day is a separate download of about 690 MB, read one at a time, so keep windows to days or a few weeks.
- Days with no published granule are left out, not filled; stderr lists them as trailing (likely not yet published) or interior gaps. A window with no granule exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-10-01 --end-time 2026-10-07 \
  --bbox 5.5/33.5/-5/42.5 -o smap_kenya.zarr
```
Writes seven daily AM soil-moisture maps (m3 m-3) over Kenya.

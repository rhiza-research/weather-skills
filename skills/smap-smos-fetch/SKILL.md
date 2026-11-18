---
name: smap-smos-fetch
description: "Fetch daily surface soil moisture (m3 m-3) from SMAP, SMOS, or both (SMAP with gaps filled from SMOS), for the 6am or 6pm overpass or both, a date range and optional region, as a weather-skills Zarr. Native products (SMAP SPL3SMP_E 9 km, 2 days behind; CATDS SMOS 25 km, 2010 on, a week behind) or NASA's intercalibrated NSIDC-0800 (both on SMAP's 9 km grid without a bias between them, 2015 to about 5 weeks ago). Needs an Earthdata login except for native SMOS. Use when a task needs gridded soil-moisture observations, e.g. for drought or agricultural analysis, or comparison against rainfall or models."
license: MIT
compatibility: Requires Python 3.12 and uv. SMAP and the intercalibrated product authenticate to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`. Earthdata accounts are free with sign-up at https://urs.earthdata.nasa.gov/users/new. Native SMOS reads open HTTPS files from CATDS (https://data.catds.fr) and needs no credentials; CATDS asks that publications acknowledge it (the text is in the output's `acknowledgement` attribute).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - soil_moisture
    - soil_moisture_source
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# smap-smos-fetch

Reads one global file per day (and per overpass for SMOS) and keeps the requested box: NASA files are read over HTTPS a slice at a time, CATDS files are downloaded and cut. Writes one daily series.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [--sensor smap|smos|both] [--product native|intercalibrated] [--overpass AM|PM|both] [--quality recommended|all] -o OUT.zarr
# Latest day every needed source has (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--sensor ...] [--product ...] [--overpass ...]
```

Failed Earthdata login exits 1 with a hint; it never prompts.

### Arguments
- `--start-time`, `--end-time` — inclusive range of days. Every day is in the output; days with no file, an empty file, or nothing usable in the box are NaN, with a note on stderr.
- `--bbox` — `N/W/S/E` decimal degrees; W > E crosses the antimeridian. Default: the globe.
- `--sensor` — `smap`, `smos`, or `both`: SMAP where it has a value, else SMOS. Default: `smap`.
- `--product` — which retrievals:
  - `native` (default): SMAP's enhanced L3 (`SPL3SMP_E`, 2015-03-31 on, about 2 days behind) and ESA's SMOS L3 from CATDS (`MIR_CLF31A`/`D`, 2010-01-17 on, about a week behind). Each is its own team's retrieval, so they disagree: over Kenya, SMAP − SMOS was −0.06 ± 0.12 m3 m-3.
  - `intercalibrated`: NASA's NSIDC-0800 v2 (2015-03-31 to about 5 weeks ago), where SMOS is retrieved with SMAP's algorithm from SMOS measurements calibrated to SMAP. Over Kenya the two differed by 0.001 ± 0.011 m3 m-3. Use it when a consistent SMAP+SMOS record matters.
- `--overpass` — `AM` (6am local), `PM` (6pm local), or `both`: AM, with its gaps filled from PM. Default: `AM`.
- `--quality` — `recommended` keeps only NASA retrievals flagged recommended quality; `all` also keeps lower-quality ones (e.g. dense vegetation), often more coverage. CATDS SMOS is kept as served. Default: `recommended`.
- `--probe-latest` — print the latest day (`YYYY-MM-DD`) that every source the other options need has, and exit. Takes an optional id that this skill ignores.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `soil_moisture` on `(time, latitude, longitude)` in `m3 m-3` (`volume_fraction_of_condensed_water_in_soil`), `data_interval="1 day"`. NaN marks water, no retrieval, values outside 0.02–0.5, and (with `--quality recommended`) lower-quality NASA retrievals.
- `soil_moisture_source`, when more than one retrieval or overpass is combined: `1` SMAP AM, `2` SMOS AM, `3` SMAP PM, `4` SMOS PM, `0` none. Each cell takes the first of these, in that order, that has a value; values are not adjusted.
- The grid: EASE-Grid 2.0 9 km cell centres (longitude every ~0.09°, latitude spacing varies; ascending), the same for every option except native SMOS alone, which stays on its 25 km EASE-2 grid (~0.26° longitude). With `--sensor both --product native`, each 9 km cell takes the 25 km SMOS cell its centre is nearest to. Run coarsen to put it on another grid.
- A `latitude_longitude` grid-mapping variable (WGS84). `weather_skills_source`: `nsidc:SPL3SMP_E`, `catds:smos-l3sm`, `nsidc:SPL3SMP_E+catds:smos-l3sm`, or `nsidc:NSIDC-0800:<sensor>`.

### Notes
- One overpass covers a given place every 2–3 days per satellite, so a single day's map is patchy. Over Kenya, 10–12 March 2025, `--sensor both --overpass both` covered 44–90% of the box per day natively and 29–87% intercalibrated. Average a few days (aggregate-temporal) for full maps.
- Cells are empty over water, dense forest, frozen ground and radio-interference hotspots.
- Each day reads only the box from the NASA files (~10 s a day over Kenya). Native SMOS downloads a ~7 MB global file per day and overpass: a year of one overpass is about 2.5 GB.
- Some NSIDC-0800 granules are empty placeholders, and a few days have none (9 in Feb–Mar 2024); their days come out NaN.
- ascat-fetch's soil moisture is % of saturation, not m3 m-3: compare the two by anomalies or ranks.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2025-10-01 --end-time 2025-12-31 --bbox 5.5/33.5/-5/42 \
  --sensor both --product intercalibrated --overpass both -o kenya_ond2025_sm.zarr
```
Writes daily soil moisture over Kenya for October–December 2025: SMAP filled in with SMOS, 6am filled in with 6pm, without a bias between the sensors.

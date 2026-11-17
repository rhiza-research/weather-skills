---
name: smap-smos-fetch
description: "Fetch NASA's intercalibrated SMAP/SMOS daily 9 km surface soil moisture (NSIDC-0800 v2, m3 m-3, 31 March 2015 to about 5 weeks ago) for the 6am or 6pm overpass or both, a date range and optional region, as a weather-skills Zarr: SMAP with its gaps filled from SMOS retrieved the same way, or either one alone. Needs an Earthdata login. Use when a task needs the most complete daily SMAP-type soil moisture record, or SMAP and SMOS without a bias between them."
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`. Earthdata accounts are free with sign-up at https://urs.earthdata.nasa.gov/users/new.
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

Reads NSIDC-0800 granules over HTTPS, taking only the box's rows and columns of the soil moisture fields, and writes one daily series.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [--overpass AM|PM|both] [--source merged|smap|smos] [--quality recommended|all] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive range of days, from 2015-03-31. Every day is in the output; days with no granule, or an empty one, are NaN, with a note on stderr.
- `--bbox` — `N/W/S/E` decimal degrees; W > E crosses the antimeridian. Default: the globe.
- `--overpass` — `AM` (6am local), `PM` (6pm local), or `both`: AM, with its gaps filled from PM. Default: `AM`.
- `--source` — `merged`: SMAP where it has a value, else SMOS; `smap` or `smos`: that retrieval only. Default: `merged`.
- `--quality` — `recommended` keeps only retrievals flagged recommended quality; `all` keeps every retrieval in 0.02–0.5 m3 m-3. Default: `recommended`.
- `--probe-latest` — print the last day NSIDC has (`YYYY-MM-DD`) and exit.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `soil_moisture` on `(time, latitude, longitude)` in `m3 m-3` (`volume_fraction_of_condensed_water_in_soil`), the same quantity and units as smap-fetch and smos-fetch.
- `soil_moisture_source`, when more than one retrieval or overpass is combined: `1` SMAP AM, `2` SMOS AM, `3` SMAP PM, `4` SMOS PM, `0` none. Each cell takes the first of these, in that order, that has a value.
- The native 9 km EASE-Grid 2.0: longitude every ~0.09°, latitude spacing varies with latitude. Run coarsen to put it on another grid.
- `weather_skills_source=nsidc:NSIDC-0800:<source>`.

### Notes
- The SMOS values are made with SMAP's algorithm from SMOS measurements calibrated to SMAP, so the two agree closely where both exist. Over Kenya, 1–7 Oct 2026 (6am), they differed by 0.001 ± 0.011 m3 m-3, against 0.06 ± 0.12 for SMAP against smos-fetch's CATDS SMOS. Use this skill rather than interleaving smap-fetch and smos-fetch when a consistent record matters.
- One overpass covers a given place every 2–3 days per satellite. Over Kenya, 1–7 Oct 2026, the share of land cell-days with a value was 41% for AM, 25% for PM, and 48% for `both`: the evening pass mostly repeats the morning strips. A day with nothing is normal; average a few days (aggregate-temporal) for full maps.
- With `both`, PM values fill AM gaps unadjusted. Where both passes saw a cell the same day (Kenya, Oct 2026), AM was wetter by 0.02 ± 0.05 m3 m-3.
- The record runs about 5 weeks behind. For recent days use smap-fetch (2 days behind) or smos-fetch (about a week).
- Some granules are empty placeholders; their days come out NaN, even when smap-fetch has data for them.
- Each day reads only the needed fields from a ~1.7 GB file: a week over Kenya takes about a minute and a half.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2025-10-01 --end-time 2025-12-31 --bbox 5.5/33.5/-5/42 -o kenya_ond2025_sm.zarr
```
Writes daily 6am soil moisture over Kenya for October–December 2025, SMAP filled in with SMOS.

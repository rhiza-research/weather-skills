---
name: smos-fetch
description: "Fetch ESA SMOS daily L3 surface soil moisture (m3 m-3, ~25 km EASE-2 grid, 2010 to about a week ago) for one overpass (6am or 6pm), a date range and optional region from CATDS (CNES), as a weather-skills Zarr. No credentials needed. Use when a task needs SMOS soil moisture, or an L-band estimate in the same units as smap-fetch to compare with it."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads open HTTPS files from CATDS (https://data.catds.fr); no credentials required. CATDS asks that publications acknowledge it (the text is in the output's `acknowledgement` attribute).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - soil_moisture
---

# smos-fetch

Downloads one global CATDS L3 file per day for the chosen overpass, cuts the box, and writes one daily series.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [--overpass AM|PM] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--overpass AM|PM]
```

### Arguments
- `--start-time`, `--end-time` — inclusive range of days, from 2010-01-17. Days CATDS has no file for are skipped, with a note on stderr.
- `--bbox` — `N/W/S/E` decimal degrees. Default: the globe.
- `--overpass` — `AM` (6am local, ascending) or `PM` (6pm local, descending). Default: `AM`, the one less disturbed by daytime heating.
- `--probe-latest` — print the last day CATDS has for `--overpass` (`YYYY-MM-DD`) and exit.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `soil_moisture` on `(time, latitude, longitude)` in `m3 m-3` (`volume_fraction_of_condensed_water_in_soil`), the same quantity and units as smap-fetch.
- The native EASE-2 grid: longitude every ~0.26°, latitude spacing varies with latitude (~0.2° in the tropics). Run coarsen or downscale to put it on another grid.
- `weather_skills_source=catds:smos-l3sm:MIR_CLF31A` (AM) or `MIR_CLF31D` (PM); `acknowledgement` holds the text CATDS asks publications to carry.

### Notes
- One overpass covers a given place every 2–3 days, so a single day's map is patchy. Average a few days (aggregate-temporal) for a full map.
- Cells are empty over water, dense forest, frozen ground and radio-interference hotspots.
- Days to 2021-05-24 come from the reprocessed RE07 archive and later days from the operational files; both use the same retrieval (v7).
- Each day downloads a ~7 MB global file; a year of one overpass is about 2.5 GB.
- The newest data is about a week old.
- ascat-fetch's soil moisture is % of saturation, not m3 m-3: compare the two by anomalies or ranks.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-10-01 --end-time 2026-10-31 --bbox 5.5/33.5/-5/42 -o kenya_smos_oct.zarr
```
Writes October 2026 SMOS morning-overpass soil moisture over Kenya.

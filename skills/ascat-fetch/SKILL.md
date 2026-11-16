---
name: ascat-fetch
description: "Fetch daily ASCAT satellite surface soil moisture (percent of saturation, 0.25° global, 1991 to about a week ago) for a date range and optional region from the Copernicus Climate Data Store's C3S ACTIVE product (Metop ASCAT, ERS scatterometers before 2007), as a weather-skills Zarr. Needs a free CDS key. Use when a task needs ASCAT or scatterometer soil moisture, e.g. for drought, crop or flood analysis, or to compare with SMAP or rainfall."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires CDS_API_KEY (or a ~/.cdsapirc with a `key:`; an ADS key is the same ECMWF token and works), free with sign-up — register at https://cds.climate.copernicus.eu and copy the key from https://cds.climate.copernicus.eu/profile. The satellite-soil-moisture licence must be accepted once on the CDS website.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - soil_moisture_saturation
    - soil_moisture_saturation_uncertainty
  openclaw:
    requires:
      env:
        - CDS_API_KEY
    primaryEnv: CDS_API_KEY
    envVars:
      - name: CDS_API_KEY
        description: Personal token from https://cds.climate.copernicus.eu/profile
---

# ascat-fetch

Requests the daily C3S ACTIVE soil moisture files from the CDS, one request per month, cuts the box after download, and writes one daily series.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [--uncertainty] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive range of days, from 1991-08-05. Days the CDS does not list are skipped, with a note on stderr.
- `--bbox` — `N/W/S/E` decimal degrees. Default: the globe.
- `--uncertainty` — also write `soil_moisture_saturation_uncertainty`.
- `--probe-latest` — print the last day the CDS lists (`YYYY-MM-DD`) and exit. Needs no key.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `soil_moisture_saturation` on `(time, latitude, longitude)`, daily, 0.25°, in `percent`: how wet the top ~2 cm of soil is relative to its own wettest (100 = saturated). Not volumetric (m3 m-3), so it does not compare with smap-fetch's values directly; compare anomalies or ranks.
- `weather_skills_source=cds:satellite-soil-moisture:active`; `source` names the product versions used.

### Notes
- Each day is the CDS's newest version for it (e.g. `v202505_2` from September 2026). The record is reprocessed yearly, so values can change between versions.
- One value per cell per day: the product merges the day's ascending and descending passes (and Metop-B and -C). There is no per-overpass field.
- Cells are empty over water, dense forest, snow or frozen ground, and on days no pass covered them. Expect gaps of a day or two between passes.
- The CDS cannot cut a box, so each day downloads a ~1 MB global file; a year is about 400 MB.
- Latest data is about 6 days behind.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-10-01 --end-time 2026-10-31 --bbox 5.5/33.5/-5/42 -o kenya_sm_oct.zarr
```
Writes daily ASCAT soil moisture over Kenya for October 2026.

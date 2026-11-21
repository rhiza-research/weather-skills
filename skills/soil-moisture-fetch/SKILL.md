---
name: soil-moisture-fetch
description: "Fetch daily satellite surface soil moisture for a date range and optional region as a weather-skills Zarr, from one of: ASCAT (C3S ACTIVE, % of saturation, 0.25°, 1991 on), C3S COMBINED (active and passive merged, m3 m-3, 0.25°, 1978 on), SMAP (SPL3SMP_E, m3 m-3, 9 km, 2015 on), SMOS (CATDS L3, m3 m-3, 25 km, 2010 on), or NASA's intercalibrated SMAP+SMOS (NSIDC-0800, 9 km, SMAP gaps filled from SMOS). SMAP and SMOS can take the 6am or 6pm overpass or both. Use when a task needs gridded soil-moisture observations, e.g. for drought, crop or flood analysis, or comparison against rainfall or models."
license: MIT
compatibility: Requires Python 3.12 and uv. ascat and combined need a free CDS key in CDS_API_KEY (or a ~/.cdsapirc whose url is the CDS) — register at https://cds.climate.copernicus.eu, copy the key from https://cds.climate.copernicus.eu/profile, and accept the satellite-soil-moisture licence once on the website. smap and smap-smos need a free NASA Earthdata login (https://urs.earthdata.nasa.gov/users/new) in EARTHDATA_USERNAME and EARTHDATA_PASSWORD (or EARTHDATA_TOKEN), or a `.netrc` entry for `urs.earthdata.nasa.gov`. smos reads open HTTPS files from CATDS (https://data.catds.fr) and needs no credentials; CATDS asks that publications acknowledge it (the text is in the output's `acknowledgement` attribute).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - soil_moisture
    - soil_moisture_saturation
  openclaw:
    requires:
      env:
        - CDS_API_KEY
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# soil-moisture-fetch

One fetcher for five satellite soil-moisture records. Reads one global file per day (per overpass for SMOS), keeps the requested box, and writes one daily series with every requested day in it.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ascat|combined|smap|smos|smap-smos --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [--overpass AM|PM|both] [--quality recommended|all] [--uncertainty] -o OUT.zarr
# Latest day the dataset has (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset DATASET [--overpass ...] --probe-latest
```

### Datasets
| `--dataset` | What | Units, grid | Record | Credentials |
|---|---|---|---|---|
| `ascat` | C3S ACTIVE: Metop ASCAT (ERS AMI before 2007), each day's passes merged | % of saturation, 0.25° | 1991-08-05 to ~6 days ago | CDS |
| `combined` | C3S COMBINED: active and passive sensors (ASCAT, AMSR2, SMAP, SMOS, GPM, ...) merged | m3 m-3, 0.25° | 1978-11-01 to ~6 days ago | CDS |
| `smap` | NASA SMAP enhanced L3 (`SPL3SMP_E`) | m3 m-3, EASE-2 9 km | 2015-03-31 to ~2 days ago | Earthdata |
| `smos` | ESA SMOS L3 from CATDS (`MIR_CLF31A`/`D`) | m3 m-3, EASE-2 25 km | 2010-01-17 to ~a week ago | none |
| `smap-smos` | NASA NSIDC-0800 v2: SMAP, gaps filled from SMOS retrieved with SMAP's algorithm | m3 m-3, EASE-2 9 km | 2015-03-31 to ~5 weeks ago | Earthdata |

`smap` and `smos` are each their own team's retrieval and disagree (over Kenya, SMAP − SMOS was −0.06 ± 0.12 m3 m-3); in `smap-smos` the two differed by 0.001 ± 0.011. Use `smap-smos` when a consistent SMAP+SMOS record matters.

### Arguments
- `--dataset` — required; see the table.
- `--start-time`, `--end-time` — inclusive range of days. Every day is in the output; days with no file or nothing usable in the box are NaN, with a note on stderr.
- `--bbox` — `N/W/S/E` decimal degrees; W > E crosses the antimeridian. Default: the globe.
- `--overpass` — `smap`, `smos`, `smap-smos` only: `AM` (6am local, default), `PM` (6pm), or `both`: AM, gaps filled from PM. `ascat` and `combined` merge each day's passes and reject it.
- `--quality` — `smap`, `smap-smos` only: `recommended` (default) keeps only retrievals flagged recommended quality; `all` also keeps lower-quality ones (e.g. dense vegetation), often more coverage.
- `--uncertainty` — `ascat`, `combined` only: also write `<variable>_uncertainty`.
- `--probe-latest` — print the latest day (`YYYY-MM-DD`) the dataset (and overpasses) has, and exit. Takes an optional id that this skill ignores.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `soil_moisture` on `(time, latitude, longitude)` in `m3 m-3` (`volume_fraction_of_condensed_water_in_soil`), or for `ascat`, `soil_moisture_saturation` in `percent` (100 = the wettest that cell gets; not volumetric, so compare it with the others by anomalies or ranks). `data_interval="1 day"`.
- `soil_moisture_source`, when more than one retrieval or overpass is combined: the 1-based place in `flag_meanings` (`smap_am smos_am smap_pm smos_pm`, as requested) of the retrieval each value came from, `0` none. Values are not adjusted.
- The grid is each product's own: 0.25° for `ascat` and `combined`; EASE-Grid 2.0 cell centres for the others (longitude even, latitude spacing varies). Run coarsen to put it on another grid.
- `weather_skills_source`: `cds:satellite-soil-moisture:active|combined`, `nsidc:SPL3SMP_E`, `catds:smos-l3sm`, or `nsidc:NSIDC-0800`.

### Notes
- A radiometer overpass covers a place every 2–3 days, so one day of `smap` or `smos` is patchy; `--overpass both` and `smap-smos` fill more. ASCAT covers about three quarters of Kenya on a given day; COMBINED nearly all of it. Average a few days (aggregate-temporal) for full maps.
- Cells are empty over water, dense forest, frozen ground and radio-interference hotspots.
- Download size: CDS and CATDS files are global (CDS ~1 MB a day, a year ~400 MB; CATDS ~7 MB a day and overpass, a year ~2.5 GB). NASA files are read only over the box (~10 s a day over Kenya).
- The C3S record is reprocessed yearly; each day is the CDS's newest version (in the `source` attribute), so values can change between versions.
- Some NSIDC-0800 granules are empty placeholders; their days come out NaN.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset smap-smos --overpass both --start-time 2025-10-01 --end-time 2025-12-31 \
  --bbox 5.5/33.5/-5/42 -o kenya_ond2025_sm.zarr
```
Writes daily soil moisture over Kenya for October–December 2025: SMAP filled in with SMOS, 6am filled in with 6pm, without a bias between the sensors.

---
name: cams-fetch
description: "Fetch CAMS global air-quality fields (PM2.5, PM10, near-surface NO2, O3, SO2, CO, aerosol optical depth, total columns) for a lat/lon box from the Copernicus Atmosphere Data Store, as a gridded weather-skills Zarr: the 5-day forecast (0.4°, 2015 on) or the EAC4 reanalysis (0.75°, 3-hourly, 2003 on). Needs a free ADS_API_KEY. Use when a task needs gridded air-quality or atmospheric-composition data anywhere in the world."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires ADS_API_KEY (or a ~/.cdsapirc whose url is the ADS), free with sign-up — register at https://ads.atmosphere.copernicus.eu and copy the key from https://ads.atmosphere.copernicus.eu/profile and the eccodes library that cfgrib loads (installed with the skill on macOS and Linux). Each dataset's licence must be accepted once on the ADS website.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - pm25
    - pm10
    - no2
    - o3
    - aod550
    - so2
    - co
    - pm1
    - duaod550
    - tcno2
    - tco3
    - tcco
    - tcso2
  openclaw:
    requires:
      env:
        - ADS_API_KEY
    primaryEnv: ADS_API_KEY
    envVars:
      - name: ADS_API_KEY
        description: Personal token from https://ads.atmosphere.copernicus.eu/profile
---

# cams-fetch

Requests CAMS fields from the ADS with the box cut server-side, so only the region is downloaded, and writes one variable per field.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox N/W/S/E --date YYYY-MM-DD [--cycle 0|12] [--max-lead 48h] [-v VAR ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset eac4 --bbox N/W/S/E --start-time YYYY-MM-DD --end-time YYYY-MM-DD [-v VAR ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [forecast|eac4]
```

### Arguments
- `--bbox` — `N/W/S/E` decimal degrees. Required. A box crossing the antimeridian (W > E) is sent as two requests.
- `--dataset` — `forecast` (CAMS global composition forecast) or `eac4` (reanalysis). Default: `forecast`.
- `--date` — forecast only: init day, `YYYY-MM-DD`, from 2015-01-01.
- `--cycle` — forecast only: init hour UTC, `0` or `12`. Default: `0`.
- `--max-lead` — forecast only: last lead, as hours (`48`) or a timedelta (`48h`, `2D`), up to `120h`. Leads are 3-hourly. Default: `120h`.
- `--start-time`, `--end-time` — eac4 only: inclusive range of days, from 2003-01-01. All eight 3-hourly analyses per day; one ADS request per calendar month.
- `--variable`, `-v` — fields: `pm25`, `pm10`, `no2`, `o3`, `aod550`, `so2`, `co`, `pm1`, `duaod550`, `tcno2`, `tco3`, `tcco`, `tcso2`. Repeat the flag for more. Default: `pm25`.
- `--probe-latest` — print the last day the ADS catalogue lists (`YYYY-MM-DD`) for IDENT `forecast` or `eac4` (default: `--dataset`) and exit. Needs no key.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `forecast`: dims `(step, latitude, longitude)`, scalar `time` = init, `valid_time(step)`. `eac4`: dims `(time, latitude, longitude)`, 3-hourly.
- Units: `pm*` in `ug m-3` (from kg m-3); gases in `nmol mol-1` (ppb), converted from mass mixing ratio on the lowest model level (~10 m) and tagged `model_level`; `*aod550` in `1`; `tc*` in `kg m-2`.
- CF `standard_name`, `long_name`, and `ads_variable` on each field; `data_interval` stamped; `weather_skills_source=cams:<ADS dataset id>`.

### Notes
- Set `ADS_API_KEY` in the environment on the first call (free; register at https://ads.atmosphere.copernicus.eu and copy the key from your profile). A missing key exits 2 before any request. Never print the value.
- The first request for each dataset fails until its licence is accepted in a browser; the error prints the licence URL.
- The ADS queues each request: seconds to many minutes. A job still queued after 1 hour exits 1; re-run later or request less.
- A date outside the product's range exits 1 with the ADS message; check `--probe-latest`. EAC4 runs several months behind real time.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.1/33.9/-4.8/41.9 --date 2026-10-05 --cycle 12 \
  --max-lead 48h -v pm25 -v no2 -v o3 -o cams_kenya.zarr
```
Writes the first 48 h of the 12 UTC forecast of PM2.5 and near-surface NO2 and O3 over Kenya, 3-hourly.

## Details

| `--dataset` | ADS dataset | Grid | Time axis |
|---|---|---|---|
| `forecast` | `cams-global-atmospheric-composition-forecasts` | 0.4° | one init, leads 0–120 h |
| `eac4` | `cams-global-reanalysis-eac4` | 0.75° | 3-hourly analyses |

The lowest model level is 137 for the forecast and 60 for EAC4. Gases use molar masses NO2 46.0055, O3 47.9982, SO2 64.066, CO 28.0101 and dry air 28.9647 g mol-1. Variable names match those of OpenAQ station data, and station networks often report gases in ppm or µg m-3, so convert units before comparing. The European 0.1° regional ensemble is not served.

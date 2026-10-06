---
name: cams-fetch
description: "On the first call (not --probe-latest), inject secret ADS_API_KEY — do not run once without it and retry. Fetch CAMS global air-quality fields (PM2.5, PM10, near-surface NO2/O3/SO2/CO, aerosol optical depth, total columns) from the Copernicus Atmosphere Data Store for a --bbox and write a gridded weather-skills standard dataset Zarr. --dataset forecast (default; 0.4°, 00/12 UTC inits, 5-day leads, 2015+) takes --date; --dataset eac4 (reanalysis, 0.75°, 3-hourly, 2003+) takes --start-time/--end-time. Default -v pm25. PM is written in ug m-3 and gases in nmol mol-1 (ppb). Use for gridded air quality anywhere, including Africa; for station observations use openaq-fetch. Country bbox: resolve-region first."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`) and ADS_API_KEY (or a ~/.cdsapirc whose url is the ADS). The ADS URL (`https://ads.atmosphere.copernicus.eu/api`) is hardcoded; the key is the personal token from your ADS profile. Each dataset's licence must be accepted once on the ADS website.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
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

Retrieves Copernicus Atmosphere Monitoring Service (CAMS) global
atmospheric-composition fields from the Atmosphere Data Store (ADS) and writes
a weather-skills standard dataset Zarr. The bbox is cut **server-side** (ADS
`area` key), so only the region is downloaded. The request is queued at the
ADS, so expect anywhere from seconds to many minutes before the download
starts.

Two products:

| `--dataset` | ADS dataset | Grid | Time axis | Range |
|---|---|---|---|---|
| `forecast` (default) | `cams-global-atmospheric-composition-forecasts` | 0.4° | one init (`--date` + `--cycle`), leads 0–120 h | 2015-01-01 → today |
| `eac4` | `cams-global-reanalysis-eac4` | 0.75° | 3-hourly analyses | 2003-01-01 → several months behind real time (`--probe-latest eac4`) |

## When to use

- Gridded air quality (PM2.5, PM10, NO2, O3, SO2, CO, AOD) for any region,
  including where there are few or no ground stations.
- `forecast` for "what is air quality now / over the next 5 days"; `eac4` for
  history and climatology.
- Compare with OpenAQ stations: `openaq-fetch` → `point-value` on this Zarr →
  `difference`. Variable names (`pm25`, `pm10`, `no2`, `o3`, `so2`, `co`)
  match openaq-fetch.

Not for Europe-only 0.1° regional forecasts (CAMS European ensemble), which
this skill does not fetch.

## Credentials

The fetch process does not inherit host secrets. On the **first** invocation,
inject `ADS_API_KEY` (free; register at https://ads.atmosphere.copernicus.eu,
then copy the key from your profile). A `~/.cdsapirc` whose `url:` is the ADS
also works; a `~/.cdsapirc` pointing at the Climate Data Store is ignored. Do
not call the skill once to discover it is missing, then retry.
`--probe-latest` needs no key. Never print, log, or echo the value.

The ADS also requires each dataset's licence to be accepted once in a browser.
If it is not, the skill exits with the licence URL to open.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox N/W/S/E --date YYYY-MM-DD [--cycle 00|12] [-v VAR ...] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset eac4 --bbox N/W/S/E --start-time YYYY-MM-DD --end-time YYYY-MM-DD [-v VAR ...] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [forecast|eac4]
```

### Arguments
- `--bbox` — required; `N/W/S/E` decimal degrees. Sent to the ADS as `area`,
  so only this region is downloaded. Antimeridian-crossing boxes (W > E) are
  split into two requests. To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--dataset` — `forecast` (default) or `eac4`.
- `--date` — forecast only: init day, absolute `YYYY-MM-DD`. Calendar day:
  `resolve-time`. Latest init: `--probe-latest`.
- `--cycle` — forecast only: init hour, `00` (default) or `12` UTC.
- `--max-lead-hours` — forecast only: last lead hour, 0–120 (default 120).
- `--lead-step` — forecast only: lead spacing in hours (default 3). `1` works
  for single-level fields (`pm*`, `*aod550`, `tc*`); gases (`no2`, `o3`,
  `so2`, `co`) are 3-hourly.
- `--start-time`, `--end-time` — eac4 only: inclusive date range, absolute
  `YYYY-MM-DD`. All eight 3-hourly analyses per day are fetched. The range is
  split into one ADS request per calendar month; a warning is printed above
  366 days.
- `--variable`, `-v` — fields to fetch. Default `pm25`. Pass several in one
  call (`-v pm25 no2`, `-v pm25 -v no2`, or `-v pm25,no2`). ADS form names
  (`particulate_matter_2.5um`) are also accepted. Unknown names exit non-zero
  and print `Available (most used first):`.
- `--probe-latest` — print the last day the ADS catalogue lists for the
  product (`YYYY-MM-DD`) on stdout and exit. Optional IDENT `forecast` or
  `eac4` (default: `--dataset`). No `-o`, no key.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Variables (most used first)

| `-v` | Field | Units written |
|---|---|---|
| `pm25` | PM2.5 mass concentration (surface) | `ug m-3` |
| `pm10` | PM10 mass concentration (surface) | `ug m-3` |
| `no2` | NO2, lowest model level (~10 m) | `nmol mol-1` (ppb) |
| `o3` | O3, lowest model level | `nmol mol-1` (ppb) |
| `aod550` | Total aerosol optical depth at 550 nm | `1` |
| `so2` | SO2, lowest model level | `nmol mol-1` (ppb) |
| `co` | CO, lowest model level | `nmol mol-1` (ppb) |
| `pm1` | PM1 mass concentration | `ug m-3` |
| `duaod550` | Dust aerosol optical depth at 550 nm | `1` |
| `tcno2` / `tco3` / `tcco` / `tcso2` | Total columns (comparable to satellite retrievals) | `kg m-2` |

PM is converted from `kg m-3` (×1e9). Gases come from the ADS as mass mixing
ratio (`kg kg-1`) on the lowest model level (137 for the forecast, 60 for
EAC4) and are converted to mole fraction with molar masses (×M_air/M_gas×1e9).
OpenAQ often reports gases in ppm or µg/m³, so run `unit-convert` before
`difference` when the units differ.

### Output

A Zarr store with one data variable per `-v`:

- `forecast`: dims `(step, latitude, longitude)`, scalar `time` = init time,
  `valid_time(step)`. Compose with `step-to-time` to compare against
  observations.
- `eac4`: dims `(time, latitude, longitude)`, 3-hourly.

Each variable carries CF `units`, `standard_name`, and `long_name`, plus
`ads_variable`. Gases also carry `model_level`. Native spacing is stamped as
`data_interval`. Stamped with `weather_skills_source=cams:<ads-dataset-id>`.

### Errors

- No key: exits 2 before any network call.
- Licence not accepted: exits non-zero with the ADS licence URL to open.
- Date outside the product's range, or a field missing for it: non-zero exit
  with the ADS message. Check `--probe-latest`.
- The job still queued after 1 hour: non-zero exit; re-run later or request less.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For this fetcher it
is a length-1 array with `input=null`. Inspect it with the `provenance` skill.

## Examples

```bash
# Today's 00 UTC PM2.5 forecast over Kenya (bbox from resolve-region)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.1/33.9/-4.8/41.9 --date 2026-10-06 -o /tmp/cams_pm25.zarr
```

```bash
# PM2.5 + near-surface NO2 and O3, first 48 h of the 12 UTC run
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.1/33.9/-4.8/41.9 --date 2026-10-05 --cycle 12 \
    --max-lead-hours 48 -v pm25 no2 o3 -o /tmp/cams_aq.zarr
```

```bash
# One month of EAC4 reanalysis PM2.5 and AOD
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset eac4 --bbox 5.1/33.9/-4.8/41.9 \
    --start-time 2024-03-01 --end-time 2024-03-31 -v pm25 aod550 -o /tmp/eac4_mar.zarr
```

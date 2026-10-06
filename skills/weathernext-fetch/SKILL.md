---
name: weathernext-fetch
description: Fetch a Google WeatherNext 2 ensemble forecast from gs://weathernext/weathernext_2_0_0/zarr/2025_to_present/<init>/predictions.zarr (the realtime archive only) and write a weather-skills standard dataset Zarr. 64 members, 6-hour leads out to 15 days, 0.25deg global grid, 13 pressure levels. Default selects every variable — pass -v and --member, the full cube is ~1 TB and store chunks span the full globe so --bbox alone does not shrink the download. `-v tp` (native `total_precipitation_6hr`, period amount converted to a rate); surface `t2m`, `u10`, `v10`, `u100`, `v100`, `msl`, `sst`; pressure-level `t`, `u`, `v`, `w`, `q`, `geopotential` (all on `level`, hPa). Requires GCS credentials (any authenticated Google Cloud identity — the bucket is not anonymous-public). Fetch writes `tp` as a per-step rate (`mm day-1`) — do not run deaccumulate after this skill.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a consolidated Zarr from gs://weathernext/weathernext_2_0_0/zarr/2025_to_present via gcsfs. Requires Google Cloud Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS or `gcloud auth application-default login`) — any authenticated Google account works, the bucket has no special allowlist.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
    - 2m_temperature
    - 10m_u_component_of_wind
    - 10m_v_component_of_wind
    - 100m_u_component_of_wind
    - 100m_v_component_of_wind
    - mean_sea_level_pressure
    - sea_surface_temperature
    - specific_humidity
    - temperature
    - u_component_of_wind
    - v_component_of_wind
    - vertical_velocity
    - geopotential
  openclaw:
    requires:
      env:
        - GOOGLE_APPLICATION_CREDENTIALS
    primaryEnv: GOOGLE_APPLICATION_CREDENTIALS
    envVars:
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON (any Google Cloud identity can read gs://weathernext)
---

# weathernext-fetch

Opens a Google WeatherNext 2 ensemble forecast Zarr from the realtime archive,
maps it onto the weather-skills standard dataset, and writes a local Zarr.

Layout:

```
gs://weathernext/weathernext_2_0_0/zarr/2025_to_present/
  YYYYMMDD_HHhr_01_preds/predictions.zarr/   # one store per init
```

Inits are **00, 06, 12, and 18 UTC**, published daily back to 2025-01-01. 64
ensemble members, 60 six-hour leads (15 days), a 0.25° global grid
(721 lat x 1440 lon), and 13 pressure levels
(50/100/150/200/250/300/400/500/600/700/850/925/1000 hPa). By default the
skill takes the **most recent** published init. Pass `--date` to pin a day
(default cycle 00 UTC).

**Scope:** this skill only reads the `2025_to_present` realtime archive.
`gs://weathernext/weathernext_2_0_0/zarr/` also has `2022_to_2023`,
`2023_to_2024`, and `2024_to_2025` folders, but those are single large
consolidated stores with a different (multi-init) layout, not the
per-init directories this skill resolves — they are out of scope here.

## When to use

- A task needs the Google WeatherNext 2 (GenCast-lineage) ensemble forecast
  — precipitation, temperature, wind, pressure, humidity, or geopotential —
  already published under `gs://weathernext`.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills standard dataset Zarr, e.g. against `arco-era5-fetch` (same
  ERA5-lineage variable names and units) or `chirps-fetch`/`imerg-fetch` for
  precip verification.

Not in the dynamical.org catalog — this is the source-specific fetcher.
Prefer `ecmwf-fetch` for ECMWF S2S, `neuralgcm-fetch` for NeuralGCM S2S. This
skill is **Google WeatherNext 2 only**.

## Credentials

The bucket requires an authenticated Google Cloud identity (it is **not**
anonymous-public, but also not a special allowlist — any Google account with
GCS read access works). On the **first** invocation, including
`--probe-latest`, inject `GOOGLE_APPLICATION_CREDENTIALS` (path to a
service-account JSON), or rely on Application Default Credentials from
`gcloud auth application-default login`. Do not call the skill once to
discover they are missing, then retry. Never print, log, or echo the values.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD] [--cycle 00|06|12|18] \
    [--bbox N/W/S/E] [-v VAR ...] [--member N ...] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Size warning

The full cube is roughly **1 TB** (14 variables x 64 members x 60 leads x
721x1440, pressure-level fields also x13 levels). Even one surface variable,
global, all members, is ~16 GB. **The store's chunks span the full global
grid** (one chunk per member per lead, not per spatial tile), so `--bbox`
alone does **not** reduce bytes transferred — it only shrinks the written
output. To actually cut the download, pass `-v` (fewer variables) and
`--member` (fewer of the 64 ensemble members) *before* fetching; for a lead
subset, `--member` first, then the generic `select` skill (`--dim step`) on
the result.

### Arguments

- `--date` — forecast init date `YYYY-MM-DD`. Default: latest published init.
  Calendar day: `resolve-time latest`. Latest published init: `--probe-latest`.
- `--cycle` — init hour UTC, `00`/`06`/`12`/`18`. With `--date`, default `00`.
  When `--date` is omitted, the newest cycle that has a store is used.
- `--probe-latest` — print the latest init `YYYY-MM-DD` on stdout and exit.
  No `-o`.
- `--bbox` — optional spatial subset `N/W/S/E`. Native longitude is 0..360;
  negative west/east values still work. Omit for the full 0.25° global grid
  (see size warning above). Named places: compose with `resolve-region`.
- `--variable`, `-v` — restrict to named data variables (repeatable). Default
  is every field in the product (large — see size warning). Short aliases:
  `tp`/`precip`, `t2m`, `u10`, `v10`, `u100`, `v100`, `msl`, `sst`; pressure
  level `t`, `u`, `v`, `w`, `q`, `geopotential`/`z`. Native long names also
  work (e.g. `mean_sea_level_pressure`, `total_precipitation_6hr`).
- `--member` — restrict to these 0-based ensemble member indices (repeatable,
  e.g. `--member 0`). Default: all 64. Applied before download — this is the
  main way to cut read size (see size warning above).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr with dims
`(number, step, lat, lon)` and a scalar `time` coord (the init), plus
`level` for pressure-level fields. `number` is 0..63. `valid_time` (on
`step`) carries each lead's wall-clock datetime. Source precip
(`total_precipitation_6hr`) is a 6-hour **period total** in metres — fetch
writes it as `tp`, a per-step **rate** (`mm day-1`), **left-labeled** so
`step = 0` is the first 6h `[init, init+6h)`. Clip any small negative precip
at zero. Do **not** run `deaccumulate` (it is not cumulative-since-init).
For daily/weekly `mm`, `aggregate-temporal` then `convert-to-totals`.
Air/surface temperatures (`2m_temperature`, pressure-level `temperature`)
are converted to `degree_Celsius`; other variables keep native ERA5-lineage
units (`m s-1`, `Pa`, `Pa s-1`, `kg kg-1`, `m2 s-2`) and `standard_name`s,
matching `arco-era5-fetch`'s own metadata for the same fields so the two
sources compare directly. Stamped with
`weather_skills_source=weathernext:<stamp>`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For this fetcher it
is a length-1 array with `skill="weathernext-fetch"` and `input=null`. `args`
records the run's flag values under underscored names; `version` is the value
printed by `--help`. Inspect a written output's provenance with the
`provenance` skill.

## Examples

```bash
# Latest init, Kenya bbox, precip + 2m temperature, first 4 members
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 \
    -v tp -v t2m --member 0 --member 1 --member 2 --member 3 \
    -o /tmp/weathernext.zarr
```

```bash
# A pinned init, one member, 500 hPa geopotential over a bbox (adds a `level` dim)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-09-25 --cycle 12 \
    --bbox 5/34/-5/42 -v geopotential --member 0 -o /tmp/weathernext_z.zarr
uv run skills/select/scripts/select_dim.py -i /tmp/weathernext_z.zarr \
    -o /tmp/weathernext_z500.zarr --dim level --value 500
```

```bash
# Weekly precip totals then plot (single member, for a fast preview)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 \
    -v tp --member 0 -o /tmp/weathernext_m0.zarr
uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/weathernext_m0.zarr -o /tmp/weathernext_weekly.zarr --period weekly
uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/weathernext_weekly.zarr -o /tmp/weathernext_weekly_mm.zarr
uv run skills/plot/scripts/plot.py -i /tmp/weathernext_weekly_mm.zarr \
    -o /tmp/weathernext_weekly.png --spec '{"inputs":[{"variable":"tp"}]}'
```

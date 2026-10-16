---
name: weathernext-fetch
description: "Fetch a Google WeatherNext 2 AI ensemble forecast (64 members, 6-hourly leads to 15 days, 0.25° global) for one init over an optional lat/lon box, as a weather-skills Zarr: precipitation, 2 m temperature, winds, sea-level pressure, SST and pressure-level fields. Reads the 2025-to-present archive in the gs://weathernext bucket with a Google Cloud login."
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

Fetches one WeatherNext 2 ensemble forecast init from `gs://weathernext/weathernext_2_0_0/zarr/2025_to_present/` as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD] [--cycle 00|06|12|18] [--bbox N/W/S/E] -v VAR [-v VAR ...] [--member N ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--cycle HH]
```

Needs Google Cloud Application Default Credentials: set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account JSON, or run `gcloud auth application-default login`. Any Google identity can read the bucket; anonymous access fails.

### Arguments
- `--date` — init date. Default: the latest published init. Inits run daily from 2025-01-01.
- `--cycle` — init hour UTC: `00`, `06`, `12`, `18`. Default: `00` with `--date`, otherwise the newest cycle.
- `--bbox` — `N/W/S/E` decimal degrees; negative longitudes work. Default: the full global grid.
- `--variable`, `-v` — one field per flag; repeat for several. Default: every field. Aliases: `tp`/`precip`/`pr`, `t2m`, `u10`, `v10`, `u100`, `v100`, `msl`, `sst`; pressure levels `t`, `u`, `v`, `w`, `q`, `z`/`geopotential`. Native names (`mean_sea_level_pressure`) also work. An unknown name exits and lists `Available:`.
- `--member` — 0-based member index; repeat for several (negative counts from the end). Default: all 64.
- `--probe-latest [IDENT]` — print the latest init date (or `none`) and exit; honours `--cycle`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(number, step, [level,] lat, lon)` with scalar `time` = init and `valid_time` on `step`. `number` is 0..63; `level` is 13 pressure levels, 50–1000 hPa.
- `tp` is a rate in `mm day-1`, clipped at zero, labelled at the start of its 6 h period (`step=0` is `[init, init+6h)`). Other fields are instants at `init + step` (6–360 h). Fetched together, the step axis runs 0–360 h: `tp` is NaN at 360 h and the instant fields are NaN at 0 h.
- `t2m` and pressure-level `temperature` are `degree_Celsius`; SST stays `K`; others keep ERA5-style units (`m s-1`, `Pa`, `Pa s-1`, `kg kg-1`, `m2 s-2`). Stamped `weather_skills_source=weathernext:<init stamp>`.

### Notes
- Each store chunk holds the full globe for one member and lead, so `--bbox` shrinks the output but not the download. Cut the read with `-v` and `--member`: one surface field, all members, global, is ~16 GB; the full cube is ~1 TB.
- Only the per-init `2025_to_present` stores are read; the older multi-init stores in the bucket are not.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-14 --cycle 00 --bbox 5.5/33.5/-5/42.5 \
  -v tp -v t2m --member 0 --member 1 --member 2 --member 3 -o weathernext_kenya.zarr
```
Writes 4 members of 6-hourly precip rates and 2 m temperature over Kenya for 15 days.

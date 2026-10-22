---
name: windborne-sounding-fetch
description: "Fetch WindBorne WeatherMesh-6 AI forecast vertical soundings (temperature, dewpoint, specific humidity, winds and height on 21 levels, 1000–100 hPa) at chosen lat/lon locations and lead hours, as a weather-skills Zarr. Needs WindBorne API credentials. Use for skew-T style profiles, inversions or wind shear at a site; serves only about the last two days of runs."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Forecasts API (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - t
    - td
    - q
    - u
    - v
    - gh
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-sounding-fetch

Fetches WindBorne WeatherMesh-6 (`wm-6`) forecast profiles at points as a weather-skills Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point LAT,LON[,ID] [--point ...] [--date YYYY-MM-DD [--run HOUR]] [--lead HOURS ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--point` — `LAT,LON[,ID]`, repeatable; at least one. IDs default to `P0`, `P1`, …. Points outside the region the key may read return 403.
- `--date`, `--run` — init date and hour UTC (`6` or `06`). Default: the latest run. Only runs on the live list (about the last two days) are served: older runs return 404 or 400.
- `--lead` — lead hour from forecast zero, a multiple of 3 up to 360; repeatable. Default: 0.
- `--probe-latest [IDENT]` — print the latest init date on stdout and exit; stderr lists every available run and the latest one's `--date`/`--run`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(station_id, step, vertical)`; `vertical` is pressure in hPa, 1000 down to 100. `latitude` and `longitude` sit on `station_id`; scalar `time` is the run's forecast zero.
- `t`, `td` in `degree_Celsius`; `q` in `kg kg-1`; `u`, `v` in `m s-1`; `gh` is geopotential height in `m`.
- Stamps `weather_skills_source=windborne-wm6` and `windborne_initialization_time`.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment; without them it exits 2. Never write their values into files or commands.
- Every level from 1000 hPa is returned, including levels below the ground at high sites (Nairobi's surface is near 830 hPa). Those values are extrapolated; use the levels where `gh` is above the site elevation.
- Each point and lead is one request.
- For an archived run, fetch the pressure-level grid with windborne-fetch and sample it with point-value.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point -1.29,36.82,nairobi --lead 0 --lead 12 -o nairobi_sounding.zarr
```
Writes the latest run's profiles over Nairobi at forecast zero and 12 h later.

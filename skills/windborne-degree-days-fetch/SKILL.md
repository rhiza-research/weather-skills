---
name: windborne-degree-days-fetch
description: "Fetch WindBorne's WeatherMesh-6 forecast of population-weighted heating and cooling degree days (°F) for each US state, DC and Puerto Rico, daily over the next ~16 days, as a weather-skills Zarr. Needs WindBorne API credentials. Use for US energy-demand questions; it does not cover other countries."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Insights API (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - hdd
    - cdd
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-degree-days-fetch

Fetches population-weighted US heating and cooling degree days from one WeatherMesh-6 run.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD [--run HOUR]] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--date`, `--run` — init date and hour UTC (`6` or `06`). Default: the latest run. For an archived run (older than about two days), pass both `--date` and `--run`.
- `--probe-latest [IDENT]` — print the latest init date on stdout and exit; stderr lists every available run and the latest one's `--date`/`--run`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(region, time)`: `region` is the state name (50 states, District of Columbia, Puerto Rico); `time` is the calendar day.
- `hdd` and `cdd` in `degree_Fahrenheit day`, stamped `data_interval = 1 day`, `weather_skills_source=windborne-wm6` and `windborne_initialization_time`.
- Days the run lists but has no values for are left out.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment; without them it exits 2. Never write their values into files or commands.
- Not limited by the key's forecast region: it is always the US.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py -o us_degree_days.zarr
```
Writes the latest run's daily HDD and CDD for each state.

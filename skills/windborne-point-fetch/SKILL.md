---
name: windborne-point-fetch
description: "Fetch a WindBorne WeatherMesh-6 AI point forecast (3-hourly to 15 days) at chosen lat/lon locations as a weather-skills station Zarr: 2 m temperature, dewpoint, precipitation, pressure and 10/100 m winds by default, or any surface or pressure-level field with its ensemble spread, percentiles and rain-exceedance probabilities. Needs WindBorne API credentials. Use for site forecasts or meteograms; serves only about the last two days of runs."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Forecasts API (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - d2m
    - msl
    - u10
    - v10
    - u100
    - v100
    - ws10
    - ws100
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-point-fetch

Fetches a WindBorne WeatherMesh-6 (`wm-6`) forecast interpolated to points, as a station Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point LAT,LON[,ID] [--point ...] [--date YYYY-MM-DD [--run HOUR]] [-v VAR ... [--level HPA ...] [--distribution]] [--max-lead LEAD] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--point` — `LAT,LON[,ID]`, repeatable; at least one. IDs default to `P0`, `P1`, …. Points outside the region the key may read return 403.
- `--date`, `--run` — init date and hour UTC (`6` or `06`). Default: the latest run. Only runs on the live list (about the last two days) are served: older runs return 404 or 400.
- `--variable`, `-v` — default: `t2m`, `d2m`, `tp`, `msl`, `u10`, `v10`, `u100`, `v100`, `ws10`, `ws100` in one request. With `-v`, one request per field: the short names of windborne-fetch (`tmax`, `cape`, `tcc`, `ssrd`, pressure-level `t`, `z`, `q`, `u`, `v`, …) or any WindBorne variable name.
- `--level` — hPa, repeatable; required for pressure-level fields, which are written as `<name><level>` (`t850`). With no pressure-level field it exits 2.
- `--distribution` — with `-v`, also write `<name>_std`, the percentiles a field has (`<name>_p01` … `_p99`) and, for `tp`, exceedance probabilities `tp_gt_0p25mm` … `tp_gt_50mm` (0–1, per 3 h).
- `--max-lead` — last lead: hours (`48`) or a timedelta (`48h`, `2D`), a multiple of 3 h. Default: 360. `0` exits 2 when `tp` (in the default bundle) or another `_3h` field is requested: those have no value at lead 0.
- `--probe-latest [IDENT]` — print the latest init date on stdout and exit; stderr lists every available run and the latest one's `--date`/`--run`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(station_id, step)`, with `latitude` and `longitude` on `station_id`. Scalar `time` is the run's forecast zero; the init is stamped as `windborne_initialization_time`.
- Values are the ensemble mean (stamped `windborne_ensemble=mean`). Temperatures in `degree_Celsius` (spreads in `K`), pressure in `hPa`, winds in `m s-1`, `z` in `m2 s-2`.
- `tp` and its distribution, and `_3h` fields such as `tmax`, cover `[step, step + 3 h)`; `tp`, its spread and percentiles are rates in `mm day-1`. Instantaneous fields (`t2m`, `msl`, winds, …) are valid at `time + step`. With both, the interval fields are NaN at the last lead and the instantaneous ones keep lead 0.
- A field whose units the skill does not know (an unusual WindBorne name) has no `units` attr.
- Stamps `data_interval = 3 hour` and `weather_skills_source=windborne-wm6`.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment; without them it exits 2. Never write their values into files or commands.
- For an archived run, fetch the grid with windborne-fetch and sample it with point-value.
- For a full grid, use windborne-fetch; for a vertical profile, windborne-sounding-fetch.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --point -1.29,36.82,nairobi --point 0.51,35.27,eldoret -o wm6_points.zarr
```
Writes the latest run's 15-day point forecast for Nairobi and Eldoret.

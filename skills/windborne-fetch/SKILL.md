---
name: windborne-fetch
description: "Fetch WindBorne data as a weather-skills Zarr: the WeatherMesh-6 AI forecast (128-member ensemble, 3-hourly to 15 days, 0.25°) as a grid (live or archived runs back to mid-2026), at lat/lon points with spread and percentiles, or as vertical soundings; plain-language site summaries; ERA5 1991–2020 hourly normals at points; the recent ECMWF IFS analysis; or US population-weighted heating and cooling degree days. Pick one with --product. Needs WindBorne API credentials. Use when a task needs WindBorne forecasts, soundings, normals or degree days."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Forecasts and Insights APIs (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment. A free 2-week trial key (Forecasts and Insights endpoints only) is available with sign-up at https://api.windbornesystems.com/getting-started/.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
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
    - tmax
    - tmin
    - sp
    - tcwv
    - cape
    - tcc
    - ssrd
    - skt
    - t
    - z
    - q
    - u
    - v
    - td
    - gh
    - text
    - conditions
    - hdd
    - cdd
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-fetch

Fetches one WindBorne product, chosen with `--product`, as a weather-skills Zarr. Each product takes only its own flags; a flag another product uses exits 2.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product grid] [--date YYYY-MM-DD [--run HOUR]] [--bbox N/W/S/E] [-v VAR ...] [--level HPA ...] [--ensemble mean|std|members] [--max-lead LEAD] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product point --point LAT,LON[,ID] ... [--date ... [--run ...]] [-v VAR ... [--level HPA ...] [--distribution]] [--max-lead LEAD] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product sounding --point LAT,LON[,ID] ... [--date ... [--run ...]] [--lead HOURS ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product conditions --point LAT,LON[,ID] ... [--period daily|day_night|hourly] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product clim --point LAT,LON[,ID] ... --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--rolling-days N] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product analysis [--start-time YYYY-MM-DD [--end-time YYYY-MM-DD]] [--bbox N/W/S/E] [-v VAR ...] [--level HPA ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product degree-days [--date ... [--run ...]] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product P] --probe-latest
```

### Arguments
- `--product` — `grid` (default): WeatherMesh-6 gridded forecast. `point`: the forecast at points. `sounding`: vertical profiles at points. `conditions`: plain-language summaries at points, latest run only. `clim`: ERA5 1991–2020 hourly normals at points. `analysis`: ECMWF IFS deterministic analysis, the last ~25 days. `degree-days`: US state HDD/CDD.
- `--point` — `LAT,LON[,ID]`, repeatable; required for point, sounding, conditions, clim. IDs default to `P0`, `P1`, …. Forecast points outside the key's region return 403.
- `--date`, `--run` — init date and hour UTC (`6` or `06`). Default: the latest run; `--date` alone takes its latest run. `grid` also serves archived runs (back to about 1 Jul 2026); `point` and `sounding` only the live runs (about two days); `degree-days` needs `--run` for an archived run.
- `--bbox` — `N/W/S/E`, grid and analysis. Grid default: the key's region (a box past it is trimmed); analysis default: global.
- `--variable`, `-v` — repeatable. Short names: `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `u100`, `v100`, `ws10`, `ws100`, `tmax`, `tmin`, `sp`, `tcwv`, `cape`, `tcc`, `ssrd`, `skt`; pressure-level `t`, `z`, `q`, `u`, `v`. Grid and point also take any WindBorne name (kept under it; an unknown one lists them). Analysis serves `tp`, `t2m`, `d2m`, `msl`, winds, `tcc` and the pressure-level five. Default: `tp` (grid), `t2m` (analysis), and for point one request for `t2m`, `d2m`, `tp`, `msl`, `u10`, `v10`, `u100`, `v100`, `ws10`, `ws100`.
- `--level` — hPa, repeatable; required for pressure-level fields and refused without one. Grid 25 levels, analysis 20, 10–1000 hPa.
- `--ensemble` — grid: `mean` (default), `std` or `members` (all 128; only `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `u100`, `v100`, `tcc`, `z` at 500 hPa).
- `--distribution` — point, with `-v`: also `<name>_std`, percentiles `<name>_p01` … `_p99`, and for `tp` exceedance probabilities `tp_gt_0p25mm` … `tp_gt_50mm` (0–1, per 3 h).
- `--max-lead` — grid, point: hours (`48`) or a timedelta (`48h`, `2D`), a multiple of 3 h. Default: 360. `0` exits 2 with `tp` or another `_3h` field.
- `--lead` — sounding: lead hour, a multiple of 3 up to 360; repeatable. Default: 0.
- `--period` — conditions: `daily` (default, 16 days), `day_night` (split at local sunrise and sunset) or `hourly` (360 hours).
- `--start-time`, `--end-time` — inclusive dates. Clim (both required): the normals for every hour, laid on those dates. Analysis: every 00/06/12/18 UTC analysis between them; default the latest only.
- `--rolling-days` — clim: centred rolling mean over this many days. Default: 0.
- `--probe-latest [IDENT]` — print the latest init date (analysis: latest analysis date; clim: `none`, no credentials needed) and exit; stderr lists what is available. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- grid: dims `(step, latitude, longitude)`, plus `vertical` (hPa) and `number` (members). point: `(station_id, step)`, `t850`-style names for levels. sounding: `(station_id, step, vertical)` with `t`, `td`, `q`, `u`, `v`, `gh` on 21 levels 1000–100 hPa. Scalar `time` is the run's forecast zero; the init is `windborne_initialization_time`.
- conditions: `(station_id, time)` text `text` and `conditions` (labels, most notable first, `; `-joined), `end_time`, `timezone`; also printed to stderr.
- clim: `(station_id, time)` hourly from 00Z `--start-time` to 23Z `--end-time`; no `msl`. analysis: `(time, latitude, longitude)` (+ `vertical`). degree-days: `(region, time)` with `hdd`, `cdd` in `degree_Fahrenheit day` per state, DC and Puerto Rico.
- `tp` is a rate in `mm day-1` over `[time, time + interval)`; instantaneous fields are valid at their time. Temperatures in `degree_Celsius` (spreads in `K`), pressures in `hPa`, `z` in `m2 s-2`.
- Stamps `weather_skills_source` (`windborne-wm6`, `windborne-climatology` or `windborne-ecmwf-analysis`) and `data_interval`. More in [Details](#details).

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment for every call, including `--probe-latest`; without them it exits 2. Never write their values into files or commands.
- For a point or sounding from an archived run, fetch the grid (with `--level` for profiles) and sample it with point-value.
- Prefer dynamical-fetch for the ECMWF analysis when it carries what you need: it is credential-free with a longer history.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -v tp -v t2m -o wm6_kenya.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product point --point -1.29,36.82,nairobi --point 0.51,35.27,eldoret -o wm6_points.zarr
```
The first writes the latest run's 15-day ensemble-mean precipitation rate and 2 m temperature over Kenya; the second its point forecast for Nairobi and Eldoret.

## Details

### WeatherMesh-6 (grid, point, sounding)
- Runs are published most hours. For off-synoptic runs forecast zero is earlier than the init hour: the 20Z run has forecast zero 18Z.
- `tp`, `tmax`, `tmin`, `ssrd` and other `_3h` fields cover `[step, step + 3 h)`; instantaneous fields (`t2m`, `msl`, winds, `z`, …) are valid at `time + step`. With both, the `_3h` fields are NaN at the last lead and the instantaneous ones keep lead 0.
- `ssrd` stays an amount per 3 h in J m-2. Under `--ensemble std` values stay in kelvin and pascals. Stamps `data_interval = 3 hour` and `windborne_ensemble` (point is always `mean`).
- A point field whose units the skill does not know (an unusual WindBorne name) has no `units` attr.
- The grid archive is checked back to 1 Jul 2026 and lists runs from 24 Jun; archived runs download in full (every lead, members and pressure levels).
- Each grid field, level and lead is a separate request: a full run of one field is 121 requests (a few seconds). `--ensemble members` is ~7 MB per lead over the key region, about 1 GB for a full run of one field.
- Soundings return every level from 1000 hPa, including levels below the ground at high sites (Nairobi's surface is near 830 hPa). Those values are extrapolated; use the levels where `gh` is above the site elevation. Each point and lead is one request.
- Other WindBorne names include `boundary_layer_height`, `soil_moisture_level_1` and `wind_speed`.

### conditions
- Always the latest run. The text uses °F and mph, as WindBorne writes it; daily periods run between local midnights, given in UTC. A period one point lacks is an empty string. Stamps `windborne_forecast_zero` and `windborne_period`.

### clim
- Normals, not observed values; not limited to the forecast region. Three months at once works. `tp` is a rate over `[time, time + 1 h)`. Stamps `data_interval = 1 hour`, `climatology_normal` and `climatology_rolling_days`.

### analysis
- Each analysis is downloaded global (~4 MB per field) and cut locally. A range outside the ~25 served days exits 1.
- `tp` covers the hour before each analysis, labelled at its start (05Z for the 06Z analysis), stamped `data_interval = 1 hour`. The other five hours of each 6 h step are not served, so it does not add up to 6-hourly or daily totals.
- Other fields sit on the analysis times and stamp `data_interval = 6 hour` when there is more than one. With `tp` and other fields together, `time` holds both sets of times and each field is NaN at the other's.

### degree-days
- US only, whatever region your key covers. For each state (plus DC and Puerto Rico) and each forecast day: one population-weighted heating degree-day value (`hdd`) and one population-weighted cooling degree-day value (`cdd`), in °F·day. Forecast days WindBorne lists without values are dropped, not filled. Each value covers one day (`data_interval = 1 day`).

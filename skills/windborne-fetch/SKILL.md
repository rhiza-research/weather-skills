---
name: windborne-fetch
description: "Fetch a WindBorne WeatherMesh-6 AI forecast (128-member ensemble, 3-hourly leads to 15 days, 0.25°) for one init (live or archived, back to mid-2026) over the region the API key may read, as a weather-skills Zarr: ensemble mean, spread or all members of precipitation, 2 m temperature, winds, pressure and pressure-level fields. Needs WindBorne API credentials."
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
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-fetch

Fetches one WindBorne WeatherMesh-6 (`wm-6`) run from the WindBorne API as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD [--run HOUR]] [--bbox N/W/S/E] [-v VAR ...] [--level HPA ...] [--ensemble mean|std|members] [--max-lead LEAD] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--date` — init date. Default: the latest run. Older dates come from WindBorne's archive (back to about 1 Jul 2026). A date with no run exits 1.
- `--run` — init hour UTC, 0–23 (`6` or `06`). Default: the latest run on `--date`, live or archived.
- `--bbox` — `N/W/S/E`. Default: the whole region the key may read; a box reaching past it is trimmed to the overlap.
- `--variable`, `-v` — one field per flag; repeat for several. Default: `tp`. Short names: `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `u100`, `v100`, `ws10`, `ws100`, `tmax`, `tmin`, `sp`, `tcwv`, `cape`, `tcc`, `ssrd`, `skt`. Pressure-level: `t`, `z`, `q`, `u`, `v`. Any WindBorne variable name is also accepted and kept under that name; an unknown name exits and lists every available one.
- `--level` — hPa, repeatable; required for pressure-level fields; with no pressure-level field it exits 2. 25 levels, 10–1000 hPa.
- `--ensemble` — `mean` (default), `std` (ensemble spread), or `members` (all 128). Members exist only for `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `u100`, `v100`, `tcc` and `z` at 500 hPa; other fields exit 1.
- `--max-lead` — last lead: hours (`48`) or a timedelta (`48h`, `2D`), a multiple of 3 h. Default: 360 (15 days). `0` with `tp` or another `_3h` field exits 2: those fields have no value at lead 0.
- `--probe-latest [IDENT]` — print the latest init date on stdout and exit; stderr lists every available run and the latest one's `--date`/`--run`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(step, latitude, longitude)`, plus `vertical` (hPa) for pressure-level fields and `number` (0–127) for `--ensemble members`. 0.25° grid.
- Scalar `time` is the run's forecast zero, which leads count from (earlier than the init hour for off-synoptic runs); the init is stamped as `windborne_initialization_time`.
- `tp`, `tmax`, `tmin`, `ssrd` and other `_3h` fields cover `[step, step + 3 h)`; instantaneous fields (`t2m`, `msl`, winds, `z`, …) are valid at `time + step`. With both, the `_3h` fields are NaN at the last lead and the instantaneous ones keep lead 0.
- `tp` is a rate in `mm day-1`; `ssrd` stays an amount per 3 h in J m-2. `t2m`, `d2m`, `tmax`, `tmin`, `skt`, `t` are `degree_Celsius`; `msl`, `sp` are `hPa`; `z` is geopotential in `m2 s-2`. Under `--ensemble std` values stay in kelvin and pascals.
- Stamps `data_interval = 3 hour`, `weather_skills_source=windborne-wm6` and `windborne_ensemble`.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment for every call, including `--probe-latest`; without them it exits 2. Never write their values into files or commands.
- For points, soundings, plain-language conditions or climatology, use windborne-point-fetch, windborne-sounding-fetch, windborne-conditions-fetch or windborne-clim-fetch.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -v tp -v t2m -o wm6_kenya.zarr
```
Writes the latest run's 15-day ensemble-mean precipitation rate and 2 m temperature over Kenya.

## Details

- Runs are published most hours. For off-synoptic runs forecast zero is earlier than the init hour: the 20Z run has forecast zero 18Z.
- The archive is checked back to 1 Jul 2026 and lists runs from 24 Jun. Archived runs download in full (every lead, members and pressure levels); only the point and sounding endpoints refuse old runs.
- The API returns the whole region the key may read; `--bbox` is cut from it.
- Each field, level and lead is a separate request, so a full run of one field is 121 requests (a few seconds). `--ensemble members` is ~7 MB per lead over the key region: about 1 GB for a full run of one field.
- Other WindBorne names include `boundary_layer_height`, `soil_moisture_level_1` and `wind_speed`.

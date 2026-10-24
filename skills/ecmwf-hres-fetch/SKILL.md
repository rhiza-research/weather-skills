---
name: ecmwf-hres-fetch
description: "Fetch the deterministic ECMWF HRES (IFS high-resolution, single-member) forecast for one init date and run hour over a lat/lon box from the free ECMWF Open Data feed, as a weather-skills Zarr at 0.25°: precipitation, 2 m temperature, pressure, winds, radiation and pressure-level fields out to 15 days. No credentials needed. Use when a task needs the deterministic high-resolution ECMWF forecast."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). No credentials or account required — ECMWF Open Data is a public feed.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.3"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - d2m
    - msl
    - u10
    - v10
    - gust10
    - sp
    - tcwv
    - mucape
    - skt
    - ssrd
    - strd
    - str
    - ttr
    - t
    - gh
    - u
    - v
    - q
    - r
---

# ecmwf-hres-fetch

Fetches one deterministic ECMWF HRES run from ECMWF Open Data as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E [--run 0|6|12|18] [-v VAR ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--date` — init date. The skill tries `data.ecmwf.int` (last ~4 days) and then the AWS mirror (from 2023).
- `--run` — init hour UTC: `0`, `6`, `12`, or `18`. Default: `0`. 00/12 runs reach 360 h (3-hourly to 144 h, then 6-hourly); 06/18 runs reach 144 h, 3-hourly.
- `--bbox` — required for a fetch. `N/W/S/E` decimal degrees. Open Data serves global files, so the full globe is downloaded for every step and clipped locally; a small bbox saves memory, not download time (`tp` for a 00 UTC run is ~80 MB; a pressure-level field is ~13×).
- `--variable`, `-v` — one field per flag; repeat for several. Default: `tp`. Surface: `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `gust10`, `sp`, `tcwv`, `mucape`, `skt`, `ssrd`, `strd`, `str`, `ttr`. Pressure levels (1000–50 hPa, 13 levels): `t`, `gh`, `u`, `v`, `q`, `r`. Open Data names (`2t`, `10u`) are accepted. An unknown name exits and lists `Available (most used first):`.
- `--probe-latest [IDENT]` — print the latest init date on stdout and exit; stderr names its run hour. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(step, latitude, longitude)`, plus `vertical` (hPa) for pressure-level fields. Scalar `time` is the init.
- `tp` is a rate in `mm day-1` over `[step, next step)`, labelled at the interval start (`step=0` is 0–3 h). The last lead has no interval, so it is dropped — for every field when `tp` is in the request.
- `t2m`, `d2m`, `skt`, `t` are `degree_Celsius`; `gh` is metres; other units as published.
- 06/18 runs stamp `data_interval = 3 hour`; 00/12 runs, whose spacing changes at 144 h, get `step_bounds`. Stamped `weather_skills_source=ecmwf-hres`.

### Notes
- `ssrd`, `strd`, `str`, `ttr` stay cumulative since init (J m-2); deaccumulate them for a rate.
- `gust10` is the maximum gust since the previous output step.
- `tcwv` is water vapour only; `mucape` is most-unstable CAPE (Open Data has no plain `cape`).
- An init or run that neither source has exits 1. New runs publish about 6–9 h after their init hour.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-15 --run 0 --bbox 5.5/33.5/-5/42.5 -v tp -v t2m -o hres_kenya.zarr
```
Writes 15-day HRES precip rates and 2 m temperature over Kenya from the 00 UTC run.

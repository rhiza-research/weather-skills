---
name: ecmwf-fetch
description: "Fetch an ECMWF S2S (extended-range, 46-day) ensemble forecast for one init date over a lat/lon box, as a weather-skills Zarr: precipitation, 2 m temperature, SST, winds, pressure-level fields and more. Also serves inits before 2026 and fields such as ocean variables, 6-hour max/min temperature, surface fluxes and potential vorticity through the ECMWF Data Store. Use when dynamical-fetch lacks the init date or field; the Data Store needs a free ECMWF key."
license: MIT
compatibility: Requires Python 3.12 and uv. Default source is the dynamical.org catalog (no credentials). ECDS fallback requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`) and ECMWF_DATASTORES_KEY (or a `~/.ecmwfdatastoresrc` file). The store URL (`https://ecds.ecmwf.int/api`) is hardcoded in the skill, not read from the environment; the key is the personal token from your ECDS account, free with sign-up — register an ECMWF account at https://www.ecmwf.int/user/register, then copy the token from https://ecds.ecmwf.int/how-to-api.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - sst
    - d2m
    - mx2t6
    - mn2t6
    - u10
    - v10
    - msl
    - cape
    - tcw
    - t
    - gh
  openclaw:
    envVars:
      - name: ECMWF_DATASTORES_KEY
        description: Personal ECDS token (free ECMWF account at https://www.ecmwf.int/user/register). Needed only for the ECDS fallback.
---

# ecmwf-fetch

Fetches one ECMWF S2S ensemble forecast (control plus 100 members, 46 daily leads) as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

The skill reads the public dynamical.org copy (`ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree`, no credentials) when the init is 2026-01-01 or later and every requested field is in it. Otherwise it queues a request on the ECMWF Data Store (ECDS), which needs `ECMWF_DATASTORES_KEY` (your ECDS token) and can take minutes to an hour.

### Arguments
- `--date` — init date; the 00 UTC run. Inits are daily from 2023-06-27, Mondays and Thursdays before.
- `--bbox` — required. `N/W/S/E` decimal degrees; W > E crosses the antimeridian.
- `--variable`, `-v` — short names, several per flag, repeated, or comma-separated (`-v tp t2m`). Default: `tp`. Most used: `tp`, `t2m`, `sst`, `d2m`, `mx2t6`, `mn2t6`, `u10`, `v10`, `msl`, `cape`, `tcw`; pressure levels `gh`, `t`, `u`, `v`, `w`, `q`; `pv` (320 K). ECDS names (`2_m_temperature`) and GRIB names (`2t`, `wtmp`) are accepted. An unknown name exits and lists `Available (most used first):`.
- `--probe-latest [IDENT]` — print the latest init on dynamical.org and exit; no download, no credentials. `IDENT` is ignored. If the catalog is unreachable, prints today − 2 days.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(number, step, latitude, longitude)`, plus `vertical` (hPa, or K for `pv`) for level fields. `number=0` is the control, `1..100` the perturbed members. `step` is daily and labels the start of each 24 h window, so `step=0` is `[init, init+1d)`.
- `tp` is a rate in `mm day-1`; temperatures are `degree_Celsius`; other units as published. Stamped `weather_skills_source=ecmwf-s2s`.

### Notes
- ECDS refuses inits newer than today − 2 days (real-time embargo); the skill exits 1 before contacting it.
- Set `ECMWF_DATASTORES_KEY` before the first call that needs ECDS; the catalog path does not read it.
- From ECDS, pressure-level fields and `pv` are control only: they have data at `number=0` and are NaN for members 1..100.
- From ECDS, accumulated fields other than `tp` (`cp`, `sf`, `ro`, `sro`, `sshf`, `slhf`, `ssr`, `ssrd`, `str`, `strd`, `ttr`, `ewss`, `nsss`) stay cumulative since init in native units. Requesting them with `tp` exits 2.
- Ocean fields (`t20d`, `zos`, `ocu`, …) come only from ECDS on a 1.0° grid; atmosphere fields are 1.5°. Fetch them in a separate call.
- On the catalog path, lead 0 is dropped only when every requested field is empty there (daily means, `tp`). Fetch daily-mean fields apart from instant ones (`u10`, `msl`, levels) to keep both on interval-start steps.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v tp t2m -o ecmwf_s2s_kenya.zarr
```
Writes 101-member daily precip rates and 2 m temperature for 46 days over Kenya, from dynamical.org.

## Details

Full variable list, the catalog field mapping, and the ECDS request body: [references/REFERENCE.md](references/REFERENCE.md). The output carries a `weather_skills_history` provenance chain.

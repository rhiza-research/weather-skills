---
name: ecmwf-fetch
description: "Fetch an ECMWF forecast over a lat/lon box as a weather-skills Zarr, for one init date or every init in a date window: the S2S extended-range ensemble (46 days, daily or 6-hourly), the IFS-ENS 15-day 0.25° ensemble, the AIFS ensemble or single run, or the deterministic HRES. Precipitation, 2 m temperature, SST, winds, solar and thermal radiation, pressure-level fields and more. S2S inits before 2026 come from the ECMWF reforecast (10 members, 1996-2025) for precipitation, 2 m temperature and SST; other pre-2026 inits and extra fields (ocean variables, 6-hour max/min temperature, surface fluxes, potential vorticity) come from the ECMWF Data Store, which needs a free ECMWF key; everything else needs no credentials. Use for any ECMWF forecast."
license: MIT
compatibility: Requires Python 3.12 and uv. Most requests need no credentials. The ECMWF Data Store (S2S fallback) and `hres` need the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). The Data Store also needs ECMWF_DATASTORES_KEY (or a `~/.ecmwfdatastoresrc` file), the personal token of a free ECMWF account — register at https://www.ecmwf.int/user/register, then copy the token from https://ecds.ecmwf.int/how-to-api.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.10"
  catalog-group: fetchers
  variables:
    - precip
    - t2m
    - sst
    - d2m
    - mx2t6
    - mn2t6
    - u10
    - v10
    - u100
    - v100
    - gust10
    - msl
    - sp
    - cape
    - mucape
    - tcw
    - tcwv
    - skt
    - ssrd
    - strd
    - str
    - ttr
    - ssr
    - sshf
    - slhf
    - t
    - gh
    - u
    - v
    - w
    - q
    - r
  openclaw:
    envVars:
      - name: ECMWF_DATASTORES_KEY
        description: Personal ECDS token (free ECMWF account at https://www.ecmwf.int/user/register). Needed only for the S2S Data Store fallback.
---

# ecmwf-fetch

Fetches one ECMWF forecast product, chosen with `--product`, as a weather-skills standard dataset Zarr.

| `--product` | Source | Inits | Members / leads / grid |
|---|---|---|---|
| `s2s` (default) | dynamical.org; before 2026 the ECMWF reforecast, then the ECMWF Data Store (key needed) | live from 2026-01-01; see [S2S before 2026](#s2s-before-2026) | 101 (control + 100), 46 daily steps, 1.5° (reforecast 10 members; Data Store ocean fields 1.0°) |
| `s2s-6h` | dynamical.org | from 2026-01-01 | 101, 6-hourly to 46 days, 1.5° |
| `ifs-ens` | dynamical.org | from 2024-04-01 | 51 (control + 50), 3-hourly to 144 h then 6-hourly to 15 days, 0.25° |
| `aifs-ens` | dynamical.org | from 2025-07-02 | 51, 6-hourly to 15 days, 0.25° |
| `aifs-single` | dynamical.org | from 2024-04-01 | deterministic, 6-hourly to 15 days, 0.25° |
| `hres` | ECMWF Open Data | from 2023 | deterministic, 3-hourly to 144 h then 6-hourly to 360 h (06/18 UTC runs: 144 h), 0.25° |

## Usage

```bash
# One 00 UTC init
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product P] --date YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] [--max-lead 10D] -o OUT.zarr
# Every init in one or more windows
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product P --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--start-time ... --end-time ...] --bbox N/W/S/E -v VAR -o OUT.zarr
# HRES run hour (also with windows: that run of every day in them)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product hres --date YYYY-MM-DD --run 12 --bbox N/W/S/E -v precip --max-lead 72h -o OUT.zarr
# Latest init of a product; variables of a product (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product P --probe-latest
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product P --list-variables
```

### Arguments
- `--product` — one of the table above. Default `s2s`.
- `--date` — init date; the 00 UTC run (for `hres`, the run set by `--run`). S2S inits are daily from 2023-06-27, Mondays and Thursdays before.
- `--start-time`, `--end-time` — a window of inits; `--end-time` is inclusive. Repeat the pair for several windows; not with `--date`. Takes every 00 UTC init in them (for `hres`, the `--run` init of every day) with all their leads: the windows select init times, not valid times. An init with no data is skipped with a `WARNING` (never filled); no init in any window exits 1.
- `--bbox` — `N/W/S/E` decimal degrees; W > E crosses the antimeridian. Required for `s2s` and `hres`; omit it on the other products for the whole globe.
- `--variable`, `-v` — several per flag, repeated, or comma-separated (`-v precip t2m`). Default `precip`. Short names: `precip`, `t2m`, `d2m`, `sst`, `msl`, `sp`, `u10`, `v10`, `u100`, `v100`, `gust10`, `tcc`, `skt`, `cape`, `tcw`; radiation and heat fluxes `ssrd` (downward solar), `ssr`, `strd`, `str`, `ttr`, `sshf`, `slhf`; pressure levels `t`, `gh`, `u`, `v`, `w`, `q` (every level the product has). GRIB and Data Store names (`tp`, `2t`, `2_m_temperature`) and `precipitation` are accepted. The dynamical.org products also take any of their own field names (`net_long_wave_radiation_flux_top_of_atmosphere`, `temperature_850hpa`); see `--list-variables`. `s2s` fluxes are daily means in `W m-2` from 2026 and cumulative since init in J m-2 before (Data Store). `s2s` Data Store-only fields: `mx2t6`, `mn2t6`, static fields (`lsm`, `orog`, `slt`), `pv` (320 K), and ocean fields (`t20d`, `zos`, `ocu`, …). `hres` fields: `precip`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `gust10`, `sp`, `tcwv`, `mucape`, `skt`, `ssrd`, `strd`, `str`, `ttr`, and `t`, `gh`, `u`, `v`, `q`, `r` on 13 levels (1000–50 hPa). An unknown name exits 2 and lists what is available.
- `--max-lead` — keep output steps up to this lead: `24`, `24h`, `10D` (bare numbers are hours); `0` is the first step only. Default: every step.
- `--run` — `hres` only: init hour UTC, `0`, `6`, `12` or `18`. Default `0`.
- `--probe-latest [IDENT]` — print the product's latest init (`YYYY-MM-DD`, usable as `--date`) and exit. stderr notes an init whose last lead is still publishing; for `hres` it names the latest run hour. `IDENT` is ignored. If the source is unreachable, `s2s` prints today − 2 days.
- `--list-variables` — dynamical.org products: print each variable (name, units, long name) and the pressure-level fields and levels, then exit. `s2s` also lists its Data Store-only fields.
- `--output`, `-o` — output Zarr, overwritten.

Flags a product cannot honour (`--run` off `hres`; `--list-variables` on `hres`) exit 2.

### Output
- Ensembles: dims `(number, step, latitude, longitude)`; `number=0` is the control. Deterministic: `(step, latitude, longitude)`. Level fields add `vertical` (hPa; K for `pv`). One init gives a scalar `time`; init windows give a leading `time` axis, one per init.
- Variables carry the short names above; other fields keep the name they were requested by.
- `precip` is a rate in `mm day-1`; temperatures (`t2m`, `d2m`, `sst`, `skt`, `t`, …) are `degree_Celsius`; other units as published (fluxes such as OLR from dynamical.org are `W m-2` means).
- `weather_skills_source` names the source used; stderr names it too.
- Longitude is `[-180, 180)`. Missing leads or members are never padded: published leads are written as available with a `WARNING`. A `--date` with no data exits 1.

### S2S before 2026
`s2s` inits before 2026-01-01 of `precip`, `t2m` and `sst` come from the ECMWF reforecast (no credentials). The Data Store serves what it lacks: a pre-2026 `--date` that is not a reforecast init, or a field it lacks (ocean fields, `d2m`, winds, …), for one `--date` or a window.
- Reforecast inits: every second day 2006-01-03 to 2025-06-01 (`precip`, `t2m`, `sst`, one model cycle); before 2006, about daily from 1996-01-04 (`precip`, `t2m` only). The pre-2006 inits mix model cycles (stderr warns), so a trend across them can be a model change.
- Same names, dims, units and daily `step` from 0 as live `s2s`, but 10 perturbed members numbered 1..10 with no control, lining up with live members 1..100. No ensemble mean is taken.
- A request with inits on both sides of 2026-01-01 exits 2 (10 members vs 101): split it there into two calls. A pre-2026 window of `precip`, `t2m` or `sst` with no reforecast init exits 1 and prints the ranges; a window with no `sst` init (before 2006) exits 1.
- A field missing for some inits stays NaN there, with a `WARNING` listing them; nothing is filled.

### Step labels
`step` is the start of each value's interval. Averaged fields (precip, daily means, OLR) from dynamical.org have an empty lead 0; when every requested field is empty there, the skill drops it and moves each value back one lead, so `step=0` is the first interval (`[init, init+1d)` for `s2s`). Where lead spacing changes (`ifs-ens` 3- to 6-hourly, `hres` 00/12 UTC) the skill writes `step_bounds`. Instant fields (winds, pressure levels, `ifs-ens`/`aifs` `t2m`) keep their own steps; fetched together with averaged fields, lead 0 stays and the averaged field is NaN there, so fetch them in separate calls when both need interval-start steps. With `--max-lead`, an averaged field that has no value left is dropped with a warning. `hres` `precip` becomes a rate over `[step, next step)`; its last lead has no interval and is dropped (for every field when `precip` is in the request), and with `--max-lead` one more lead is fetched to keep every step up to the limit.

Example: AIFS `precip` after fetch, `step=s` covers `[s, s+6h)`. After `aggregate-temporal --period daily --time-dim step`, `step=13D` is the 24 h from lead 13 to 14 days, and `step-to-time` labels it init + 13 days, lining up with daily observations labelled by their start.

### Notes
- **Pressure levels.** `ifs-ens` and the AIFS products have `t` (850, 925 hPa) and `gh` (500, 850, 925 hPa) only. `s2s` has `t`, `gh`, `u`, `v`, `w`, `q` on 10 levels (1000–10 hPa; `q` NaN above 200 hPa), all members. `hres` has 13 levels and also `q`, `r`, `u`, `v`. `s2s-6h` has none.
- **HRES is slow.** Every lead is a whole-globe download clipped locally (`precip` for a 00 UTC run is ~80 MB; a pressure-level field ~13×); a small bbox saves memory, not time. Keep `--max-lead` short. The `ifs-ens` control member (`select` member 0) runs at HRES resolution and reads in seconds; the ensemble mean is usually the better forecast. Use `hres` when a task names HRES or needs a field or level only it has (`q`, `r`, `u`, `v` on levels, `mucape`, `tcwv`, `skt`, radiation), or a 06/12/18 UTC run.
- **HRES windows** cost inits × the single-init download, one init after another. Keep them to about a week of inits with a short `--max-lead`, or a few inits at full lead; for longer runs of inits use `ifs-ens`.
- **HRES 503s.** Under load ECMWF answers `503 Slow Down`; the skill does not wait and retry, and exits 1 saying so. Fetch one init at a time and try again in a few minutes. New runs publish about 6–9 h after their init hour.
- `hres` `ssrd`, `strd`, `str`, `ttr` stay cumulative since init (J m-2); `gust10` is the maximum since the previous step; `tcwv` is water vapour only; `mucape` is most-unstable CAPE (no plain `cape`).
- **S2S Data Store.** It refuses inits newer than today − 2 days; for a `--date` the skill exits 1 before contacting it, and in a window those inits are skipped with a `WARNING`. Each init in a window is its own queued request, and requests take minutes to over an hour, so keep Data Store windows to about 10 inits per call. Pressure-level fields and `pv` are control only there (NaN for members 1..100). Accumulated fields other than `precip` stay cumulative since init in native units; requesting them with `precip` exits 2. Ocean fields are on a 1.0° grid; fetch them in a separate call.
- The dynamical.org products read 00 UTC inits only.
- A fresh init publishes its long leads over several hours; use the previous day's init for every lead.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v precip t2m -o ecmwf_s2s_kenya.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product aifs-ens --start-time 2025-07-16 --end-time 2026-07-15 \
  --bbox 0.3/129.4/-2.4/131.65 -v precip --max-lead 14D18h -o aifs_year.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product hres --start-time 2026-10-12 --end-time 2026-10-18 \
  --bbox 5.5/33.5/-5/42.5 -v precip --max-lead 24 -o hres_kenya_week.zarr
```
The first writes 101-member daily precip rates and 2 m temperature for 46 days over Kenya; the second a year of daily AIFS ensemble inits to 14 days in one call; the third the first 24 h of precip from each 00 UTC HRES run of one week, one `time` per init.

## Details

Field mappings, the Data Store and Open Data requests, and the reforecast sources: [references/REFERENCE.md](references/REFERENCE.md). The output carries a `weather_skills_history` provenance chain.

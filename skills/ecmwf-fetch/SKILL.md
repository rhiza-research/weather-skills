---
name: ecmwf-fetch
description: "Fetch an ECMWF forecast over a lat/lon box as a weather-skills Zarr: the S2S extended-range ensemble (46 days, daily or 6-hourly), the IFS-ENS 15-day 0.25° ensemble, the AIFS ensemble or single run, or the deterministic HRES. Precipitation, 2 m temperature, SST, winds, pressure-level fields and more, for one init date or every init in a date window. S2S inits before 2026 and fields such as ocean variables, 6-hour max/min temperature, accumulated fluxes and potential vorticity come from the ECMWF Data Store, which needs a free ECMWF key; everything else needs no credentials."
license: MIT
compatibility: Requires Python 3.12 and uv. The dynamical.org products need no credentials. The ECMWF Data Store (S2S fallback) and HRES paths need the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). The Data Store also needs ECMWF_DATASTORES_KEY (or a `~/.ecmwfdatastoresrc` file); the store URL (`https://ecds.ecmwf.int/api`) is hardcoded in the skill. The key is the personal token from your ECDS account, free with sign-up — register an ECMWF account at https://www.ecmwf.int/user/register, then copy the token from https://ecds.ecmwf.int/how-to-api.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.9"
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

Fetches one ECMWF forecast product as a weather-skills standard dataset Zarr. Pick the product with `--product`; each reads its default source and falls back only to another copy of the same product.

| `--product` | Default source | Fallback | Members / leads / grid | Credentials |
|---|---|---|---|---|
| `s2s` (default) | dynamical.org `ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree` (inits from 2026-01-01) | ECMWF Data Store (ECDS): inits before 2026 and fields the catalog lacks | 101 (control + 100), 46 daily steps, 1.5° (ocean 1.0° on ECDS) | none; ECDS needs `ECMWF_DATASTORES_KEY` |
| `s2s-6h` | dynamical.org `ecmwf-ifs-ens-forecast-46-day-6-hourly-1-5-degree` (inits from 2026-01-01) | none | 101, 6-hourly to 46 days, 1.5° | none |
| `ifs-ens` | dynamical.org `ecmwf-ifs-ens-forecast-15-day-0-25-degree` (inits from 2024-04-01) | none | 51 (control + 50), 3-hourly to 144 h then 6-hourly to 15 days, 0.25° | none |
| `aifs-ens` | dynamical.org `ecmwf-aifs-ens-forecast` (inits from 2025-07-02) | none | 51, 6-hourly to 15 days, 0.25° | none |
| `aifs-single` | dynamical.org `ecmwf-aifs-single-forecast` (inits from 2024-04-01) | none | deterministic, 6-hourly to 15 days, 0.25° | none |
| `hres` | ECMWF Open Data (`data.ecmwf.int`, inits of the last 4 days) | AWS mirror (all inits from 2023) | deterministic, 3-hourly to 144 h then 6-hourly to 360 h (06/18 UTC runs: 144 h), 0.25° | none |

## Usage

```bash
# One 00 UTC init
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--product P] --date YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] [--max-lead 10D] -o OUT.zarr
# Every 00 UTC init in one or more windows (dynamical.org products)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product P --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--start-time ... --end-time ...] --bbox N/W/S/E -v VAR -o OUT.zarr
# HRES run hour
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product hres --date YYYY-MM-DD --run 12 --bbox N/W/S/E -v precip --max-lead 72h -o OUT.zarr
# Latest init of a product; catalog variables of a product (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product P --probe-latest
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product P --list-variables
```

### Arguments
- `--product` — one of the table above. Default `s2s`.
- `--date` — init date; the 00 UTC run (for `hres`, the run set by `--run`). S2S inits are daily from 2023-06-27, Mondays and Thursdays before.
- `--start-time`, `--end-time` — a window of inits; `--end-time` is inclusive. Repeat the pair for several windows. Takes every 00 UTC init in them with all their leads (the windows select init times, not valid times). Not with `--date`; not for `hres`. With `s2s`, windows read the catalog only.
- `--bbox` — `N/W/S/E` decimal degrees; W > E crosses the antimeridian. Required for `s2s` and `hres`; omit it on the other products for the whole globe.
- `--variable`, `-v` — several per flag, repeated, or comma-separated (`-v precip t2m`). Default `precip`. Short names: `precip`, `t2m`, `d2m`, `sst`, `msl`, `sp`, `u10`, `v10`, `u100`, `v100`, `gust10`, `tcc`, `skt`, `cape`, `tcw`; pressure levels `t`, `gh`, `u`, `v`, `w`, `q` (every level the product has). GRIB and ECDS names (`tp`, `2t`, `wtmp`, `z`, `2_m_temperature`) and `precipitation` are accepted. The dynamical.org products also take any catalog name (`net_long_wave_radiation_flux_top_of_atmosphere`, `temperature_850hpa`); see `--list-variables`. S2S Data Store-only fields: `mx2t6`, `mn2t6`, accumulated fluxes (`sshf`, `slhf`, `ssr`, `ssrd`, `str`, `strd`, `ttr`), static fields (`lsm`, `orog`, `slt`), `pv` (320 K), and ocean fields (`t20d`, `zos`, `ocu`, …). HRES fields: `precip`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `gust10`, `sp`, `tcwv`, `mucape`, `skt`, `ssrd`, `strd`, `str`, `ttr`, and `t`, `gh`, `u`, `v`, `q`, `r` on 13 levels (1000–50 hPa). An unknown name exits 2 and lists what is available.
- `--max-lead` — keep output steps up to this lead: `24`, `24h`, `10D` (bare numbers are hours); `0` is the first step only. Default: every step.
- `--run` — `hres` only: init hour UTC, `0`, `6`, `12` or `18`. Default `0`.
- `--probe-latest [IDENT]` — print the product's latest init (`YYYY-MM-DD`, usable as `--date`) and exit. stderr notes an init whose last lead is still publishing; for `hres` it names the latest run hour. `IDENT` is ignored. If the catalog is unreachable, `s2s` prints today − 2 days.
- `--list-variables` — dynamical.org products: print each catalog variable (name, units, long name) and the `pressure_level` group's fields and levels where the store has one, then exit. `s2s` also lists its Data Store-only fields.
- `--output`, `-o` — output Zarr, overwritten.

Flags a product cannot honour (`--run` off `hres`; windows or `--list-variables` on `hres`) exit 2.

### Output
- Ensembles: dims `(number, step, latitude, longitude)` from every source; `number=0` is the control. Deterministic: `(step, latitude, longitude)`. Level fields add `vertical` (hPa; K for `pv`). One init gives a scalar `time`; init windows give a leading `time` axis, one per init.
- Variables carry the short names above; catalog fields with no short name keep their catalog name.
- `precip` is a rate in `mm day-1`; temperatures (`t2m`, `d2m`, `sst`, `skt`, `t`, …) are `degree_Celsius`; other units as published (catalog fluxes such as OLR stay `W m-2` means).
- `weather_skills_source`: `ecmwf-s2s` for `s2s` (catalog or ECDS), `ecmwf-hres` for `hres`, `dynamical:<dataset id>` for the others.
- Longitude is `[-180, 180)`. Missing leads or members are never padded: published leads are written as available with a `WARNING`, and an init with no data exits 1.

### Step labels
`step` is the start of each value's interval. Catalog averaged fields (precip, daily means, OLR) are stored at the end of their interval with lead 0 empty; when every requested field is empty at lead 0, the skill drops it and moves each value back one lead, so `step=0` is the first interval (`[init, init+1d)` for `s2s`). Where lead spacing changes (`ifs-ens` 3- to 6-hourly, `hres` 00/12 UTC) the skill writes `step_bounds`. Instant fields (winds, pressure levels, `ifs-ens`/`aifs` `t2m`) keep their own steps; fetched together with averaged fields, lead 0 stays and the averaged field is NaN there, so fetch them in separate calls when both need interval-start steps. With `--max-lead`, an averaged field that has no value left is dropped with a warning. `hres` `precip` becomes a rate over `[step, next step)`; its last lead has no interval and is dropped (for every field when `precip` is in the request), and with `--max-lead` one more lead is fetched to keep every step up to the limit.

Example: AIFS `precip` after fetch, `step=s` covers `[s, s+6h)`. After `aggregate-temporal --period daily --time-dim step`, `step=13D` is the 24 h from lead 13 to 14 days, and `step-to-time` labels it init + 13 days, lining up with daily observations labelled by their start.

### Notes
- **Pressure levels.** `ifs-ens` and the AIFS products have `t` (850, 925 hPa) and `gh` (500, 850, 925 hPa) only, stacked onto `vertical`. `s2s` has `t`, `gh`, `u`, `v`, `w`, `q` on 10 levels (1000–10 hPa; `q` NaN above 200 hPa), all members. `hres` has 13 levels and also `q`, `r`, `u`, `v`. `s2s-6h` has none.
- **HRES is slow.** Open Data serves global GRIB files, so every lead is a whole-globe download clipped locally (`precip` for a 00 UTC run is ~80 MB; a pressure-level field ~13×); a small bbox saves memory, not time. Keep `--max-lead` short. Since IFS Cycle 48r1 the ensemble runs at HRES resolution, so the `ifs-ens` control member (`select` member 0) stands in for HRES and reads in seconds; the ensemble mean is usually the better forecast. Use `hres` when a task names HRES or needs a field or level only it has (`q`, `r`, `u`, `v` on levels, `mucape`, `tcwv`, `skt`, radiation), or a 06/12/18 UTC run.
- **HRES 503s.** Under load either source answers `503 Slow Down`. Each source gets one attempt with no waiting; if every source is rate-limiting the skill exits 1 saying so. Fetch one init at a time and try again in a few minutes. New runs publish about 6–9 h after their init hour.
- `hres` `ssrd`, `strd`, `str`, `ttr` stay cumulative since init (J m-2); `gust10` is the maximum since the previous step; `tcwv` is water vapour only; `mucape` is most-unstable CAPE (no plain `cape`).
- **S2S Data Store.** It refuses inits newer than today − 2 days (real-time embargo); the skill exits 1 before contacting it. Requests are queued and take minutes to over an hour. Pressure-level fields and `pv` are control only there (NaN for members 1..100). Accumulated fields other than `precip` stay cumulative since init in native units; requesting them with `precip` exits 2. Ocean fields are on a 1.0° grid; fetch them in a separate call.
- The dynamical.org products read 00 UTC inits only (the AIFS stores also hold 06/12/18 UTC runs).
- A fresh catalog init publishes its long leads over several hours; use the previous day's init for every lead.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v precip t2m -o ecmwf_s2s_kenya.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --product aifs-ens --start-time 2025-07-16 --end-time 2026-07-15 \
  --bbox 0.3/129.4/-2.4/131.65 -v precip --max-lead 14D18h -o aifs_year.zarr
```
The first writes 101-member daily precip rates and 2 m temperature for 46 days over Kenya; the second a year of daily AIFS ensemble inits to 14 days in one call.

## Details

The catalog field mapping, the ECDS request body and the HRES Open Data request: [references/REFERENCE.md](references/REFERENCE.md). The output carries a `weather_skills_history` provenance chain.

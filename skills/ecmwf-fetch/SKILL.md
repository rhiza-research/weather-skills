---
name: ecmwf-fetch
description: "Fetch an ECMWF S2S (extended-range, 46-day) ensemble forecast for one init date over a lat/lon box, as a weather-skills Zarr: precipitation, 2 m temperature, SST, winds, pressure-level fields and more. Also serves inits before 2026 and fields such as ocean variables, 6-hour max/min temperature, surface fluxes and potential vorticity through the ECMWF Data Store. With --hindcast, fetches the IFS extended-range hindcasts (reforecasts, inits 1996-2021) for a range of init dates instead."
license: MIT
compatibility: Requires Python 3.12 and uv. Default source is the dynamical.org catalog (no credentials); --hindcast reads the public WeatherBench2 reforecast in gs://weatherbench2 anonymously via gcsfs. ECDS fallback requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`) and ECMWF_DATASTORES_KEY (or a `~/.ecmwfdatastoresrc` file). The store URL (`https://ecds.ecmwf.int/api`) is hardcoded in the skill, not read from the environment; the key is the personal token from your ECDS account.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.3"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - sst
    - d2m
    - mx2t6
    - mn2t6
    - mx2t24
    - mn2t24
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
        description: Personal ECDS token. Needed only for the ECDS fallback.
---

# ecmwf-fetch

Fetches one ECMWF S2S ensemble forecast (control plus 100 members, 46 daily leads) as a weather-skills standard dataset Zarr, or with `--hindcast` every IFS extended-range hindcast init in a date window, stacked on a `time` axis.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --hindcast --start-time YYYY-MM-DD --end-time YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] [--issue-year YYYY] [--members] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--hindcast]
```

The skill reads the public dynamical.org copy (`ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree`, no credentials) when the init is 2026-01-01 or later and every requested field is in it. Otherwise it queues a request on the ECMWF Data Store (ECDS), which needs `ECMWF_DATASTORES_KEY` (your ECDS token) and can take minutes to an hour.

### Arguments
- `--date` — forecast init date; the 00 UTC run. Inits are daily from 2023-06-27, Mondays and Thursdays before. Required without `--hindcast`.
- `--bbox` — required. `N/W/S/E` decimal degrees; W > E crosses the antimeridian.
- `--variable`, `-v` — short names, several per flag, repeated, or comma-separated (`-v tp t2m`). Default: `tp`. Most used: `tp`, `t2m`, `sst`, `d2m`, `mx2t6`, `mn2t6`, `u10`, `v10`, `msl`, `cape`, `tcw`; pressure levels `gh`, `t`, `u`, `v`, `w`, `q`; `pv` (320 K). ECDS names (`2_m_temperature`) and GRIB names (`2t`, `wtmp`) are accepted. An unknown name exits and lists `Available (most used first):`. With `--hindcast` the fields are `tp`, `t2m`, `mx2t24`, `mn2t24`, `d2m` and `sst`; `mx2t24` and `mn2t24` exist only there.
- `--hindcast` — fetch hindcasts instead of one forecast. Takes `--start-time` and `--end-time`, not `--date`.
- `--start-time`, `--end-time` — `--hindcast` only: inclusive window of hindcast init dates (the dates the runs start from, not when they were issued).
- `--issue-year` — `--hindcast` only: the year (2016–2022) whose issues ran the hindcasts. Each issue year carries the 20 years before it. Default: the latest issue year that covers the whole window, except for `--members` `tp` from 2016 on (see Notes). For the newest model, pass `--issue-year 2022`: inits 2002–2021, all from 2022 issues. ECMWF upgrades its model during some years, so one issue year can span two model cycles; check ECMWF's IFS cycle history for the year you pick.
- `--members` — `--hindcast` only: the 10 perturbed members instead of the ensemble mean. Reads about 10 times more, except from the tiled store below.
- `--probe-latest [IDENT]` — print the latest init on dynamical.org and exit; no download, no credentials. `IDENT` is ignored. If the catalog is unreachable, prints today − 2 days. With `--hindcast`, prints the latest hindcast init.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(number, step, latitude, longitude)`, plus `vertical` (hPa, or K for `pv`) for level fields. `number=0` is the control, `1..100` the perturbed members. `step` is daily and labels the start of each 24 h window, so `step=0` is `[init, init+1d)`.
- `tp` is a rate in `mm day-1`; temperatures are `degree_Celsius`; other units as published. Stamped `weather_skills_source=ecmwf-s2s`.
- `--hindcast`: dims `(time, [number,] step, latitude, longitude)`; `time` is each hindcast's init date, with an `issue_time` coordinate (the real-time forecast date it was run alongside). Without `--members` there is no `number` dim (ensemble mean); with it, `number` is 1..10 (no control). `step` is daily from 0 to 45 and labels the start of each 24 h window. Fields: `tp`, `t2m`, `mx2t24` and `mn2t24` (24 h maximum and minimum 2 m temperature), `d2m`, `sst`. Stamped `weather_skills_source=weatherbench2:ifs-ext-reforecast` (`-ens-mean` without `--members`) and `hindcast_issue_year`; `data_interval` `1 day`. From the Sheerwater store (Notes): `weather_skills_source=sheerwater:hindcasts/ecmwf_ifs_precip`, no `issue_time` or `hindcast_issue_year`.

### Notes
- ECDS refuses inits newer than today − 2 days (real-time embargo); the skill exits 1 before contacting it.
- Set `ECMWF_DATASTORES_KEY` before the first call that needs ECDS; the catalog path does not read it.
- From ECDS, pressure-level fields and `pv` are control only: they have data at `number=0` and are NaN for members 1..100.
- From ECDS, accumulated fields other than `tp` (`cp`, `sf`, `ro`, `sro`, `sshf`, `slhf`, `ssr`, `ssrd`, `str`, `strd`, `ttr`, `ewss`, `nsss`) stay cumulative since init in native units. Requesting them with `tp` exits 2.
- `--hindcast --members` with only `tp`, no `--issue-year` and `--start-time` from 2016-01-01 reads Sheerwater's public store instead: every init date from 2016-01-01 to 2021-12-29 once, each from the newest issue that ran it, in 6° tiles so a small `--bbox` reads little. Inits from different years then come from different model cycles (issue years 2017–2022), so a trend across years can be a model change; pass `--issue-year` to keep one issue year. That store's `tp` is clipped at 0.
- Otherwise one `--hindcast` call uses one issue year, so a window may span at most 20 years; a window no issue year covers exits 2 with the coverage.
- Hindcast inits fall on the same calendar dates in every year, but on any weekday. Pair them with other models' inits by valid date, not by init.
- Hindcast `tp` at lead day 15 can be negative (down to about −50 mm day-1) where the source's accumulations are split. Values are kept as published; stderr names the lead days. Average over a week or more, or drop that step, before using daily values.
- WeatherBench2 stores whole globes: each init costs about 5 MB per variable (50 MB with `--members`) whatever the `--bbox`, so a multi-year window takes several minutes.
- Ocean fields (`t20d`, `zos`, `ocu`, …) come only from ECDS on a 1.0° grid; atmosphere fields are 1.5°. Fetch them in a separate call.
- On the catalog path, lead 0 is dropped only when every requested field is empty there (daily means, `tp`). Fetch daily-mean fields apart from instant ones (`u10`, `msl`, levels) to keep both on interval-start steps.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v tp t2m -o ecmwf_s2s_kenya.zarr
```
Writes 101-member daily precip rates and 2 m temperature for 46 days over Kenya, from dynamical.org.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --hindcast --start-time 2011-10-01 --end-time 2020-12-31 --bbox 5.5/33.5/-5/42.5 -o ecmwf_hindcast_kenya.zarr
```
Writes ensemble-mean daily precip hindcasts over Kenya for every 2022-cycle init from October 2011 to December 2020.

## Details

Full variable list, the catalog field mapping, and the ECDS request body: [references/REFERENCE.md](references/REFERENCE.md). The output carries a `weather_skills_history` provenance chain.

**Hindcasts.** `--hindcast` reads `gs://weatherbench2/datasets/ifs_extended_range/daily/ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr` (or `…-avg.zarr` with `--members`), the WeatherBench2 copy of the ECMWF IFS extended-range reforecast. It is indexed by issue date (2016-01-04 to 2022-12-29, Mondays and Thursdays) and hindcast year (−20 to −1), with each pair's real init date in a 2-D `time` coordinate; the skill picks the pairs of one issue year whose real init falls in the window. The store carries no units: precipitation is a 24 h total in metres, temperatures are 24 h means, maxima and minima in K.

**Sheerwater store.** `gs://sheerwater-public-datalake/hindcasts/ecmwf_ifs_precip.zarr` is the members' `tp` from that store flattened to one run per real init date: where several issues ran the same date, the newest is kept. Dims `(member, prediction_timedelta, lat, lon, init_time)`, 24 h totals in mm clipped at 0, 6° tiles. Read anonymously.

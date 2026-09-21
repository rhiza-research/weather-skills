---
name: ecmwf-hres-fetch
description: "Fetches the deterministic ECMWF HRES (IFS `oper`) forecast for an init date and bbox from the free ECMWF Open Data feed, writing a weather-skills standard dataset Zarr. Credential-free, no embargo (~6-9h publication lag). Default `-v tp`. Most used: `tp`, `t2m`, `msl`, `u10`, `v10`. Pressure-level: `-v t` / `-v gh` / `-v u` / `-v v` / `-v q` / `-v r` (all native Open Data levels, 1000-50 hPa). `-v` is the cfgrib short name (`t2m`, `tp`), not ARCO `2m_temperature`. Served at 0.25 degrees — the full native ~9km/0.1 degree archive is not on Open Data (needs MARS/ECDS). Fetch writes `tp` as a per-step rate (`mm day-1`) and known temperatures as `degree_Celsius` — do not run deaccumulate on `tp` after this skill. To fetch over a country, get its bbox from the resolve-region skill first."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). No credentials or account required — ECMWF Open Data is a public feed.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
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
    - cape
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

Retrieves the deterministic ECMWF HRES (`oper` stream, `fc` type) single-level
and pressure-level fields from the public
[ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data) feed
via `ecmwf-opendata`, decodes the GRIB2 with cfgrib, subsets to the requested
bbox, and writes a consolidated Zarr store. Default field is `tp`.

## When to use

- A task asks for the deterministic ECMWF forecast ("HRES", "the ECMWF
  deterministic run", "IFS high-res") for a specific init date, out to 15
  days (00/12 UTC) or 144h (06/18 UTC).
- Prefer `dynamical-fetch` (`ecmwf-ifs-ens-forecast-15-day-0-25-degree`) if an
  **ensemble** is acceptable — same underlying model, same free access, and
  its control member (`number=0`) is close to HRES but not identical
  (different post-processing pipeline).
- Prefer `ecmwf-fetch` for ECMWF **S2S / ER** (subseasonal, 46-day,
  dynamical.org catalog by default, 2-day embargo) — this skill does not
  reach that archive.

Not for reanalysis, climatology, or ensembles. It retrieves the single
deterministic HRES member only.

## Credentials

None. ECMWF Open Data is a public, unauthenticated feed (replicated across
AWS, Azure, and Google Cloud in addition to ECMWF's own servers). No
environment variables, account, or `--probe-latest`-before-first-fetch
gymnastics are needed.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E [--run 0|6|12|18] [-v VAR ...] --output <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments

- `--date` — forecast init date. Absolute ISO date `YYYY-MM-DD`. Calendar
  day: `resolve-time latest`. Latest published init: `--probe-latest`. HRES
  runs four times daily (00/06/12/18 UTC); requesting a date/run with nothing
  published yet exits non-zero with a clear message — publication lags real
  time by roughly 6-9 hours, there is no fixed multi-day embargo like S2S.
- `--run` — init hour, `0`/`6`/`12`/`18` UTC. Default `0`. The 00/12 runs
  publish the full 15-day range (3-hourly to 144h, then 6-hourly to 360h).
  The 06/18 runs (short cutoff) only publish to 144h, 3-hourly throughout.
  Every run hour is requested as `stream=oper` — there is no separate `scda`
  stream on the public feed.
- `--bbox` — required; `N/W/S/E` decimal degrees. Open Data serves whole-globe
  GRIB2 files (no server-side area subsetting) — this skill downloads the
  global grid and clips locally, so a smaller bbox only saves decode/output
  time, not download time. To fetch over a country, get its bbox from the
  `resolve-region` skill and pass the value here.
- `--variable`, `-v` — HRES field to retrieve (repeatable). Default `tp`.
  Unknown names exit non-zero and print `Available (most used first):`. Use
  the short names (`t2m`, `tp`) — not ARCO `2m_temperature` /
  `total_precipitation`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Variables (most used first)

| `-v` | Field | Notes |
|---|---|---|
| `tp` | Total precipitation | **Default.** Cumulative since step 0 on the wire; this skill deaccumulates it to a per-step rate (`mm day-1`). |
| `t2m` | 2 m temperature | `degree_Celsius`. Prefer this for "how warm". |
| `d2m` | 2 m dewpoint temperature | `degree_Celsius`. |
| `msl` | Mean sea-level pressure | |
| `u10` / `v10` | 10 m wind components | |
| `gust10` | 10 m wind gust since previous step | Cumulative-window max, left as the raw field — not deaccumulated. |
| `sp` | Surface pressure | |
| `tcwv` | Total column water vapour | Not the same quantity as S2S `tcw` (total column water, includes condensate). |
| `cape` | Convective available potential energy | |
| `skt` | Skin temperature | `degree_Celsius`. |
| `ssrd` / `strd` / `str` / `ttr` | Solar/thermal radiation fluxes | Cumulative since step 0 (J m-2); left as-is — run the `deaccumulate` skill downstream for a rate. |
| `t` | Temperature on pressure levels | 1000-50 hPa. `degree_Celsius`. |
| `gh` | Geopotential height | Same levels, metres. |
| `u` / `v` | Wind components on pressure levels | Same levels. |
| `q` | Specific humidity | Same levels. |
| `r` | Relative humidity | Same levels. |

Mixing a surface field with a pressure-level field (e.g. `-v tp -v t`) issues
two Open Data requests (one per `levtype`) and merges the results.

### Output

A Zarr store with the selected data variables and dims `(step, latitude,
longitude)` — plus `vertical` (hPa) when a pressure-level field is selected.
`step` follows the requested run's cadence (3-hourly/6-hourly for 00/12Z,
3-hourly for 06/18Z); `tp` drops its `step=0` sample once deaccumulated to a
rate. `tp` is a precipitation **rate** (`mm day-1`); known temperature fields
(`t2m`, `d2m`, `skt`, `t`) are `degree_Celsius`. Stamped with
`weather_skills_source=ecmwf-hres`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For a fetcher this
is a length-1 array; downstream zarr-writing skills append their own entry.
`args` records the run's flag values under underscored names (e.g. a flag
`--run` is recorded as `run`); `version` is the value printed by `--help`.
Inspect a written output's provenance with the `provenance` skill.

## Examples

```bash
# Default: total precipitation, over a custom bbox
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 23/-20/-37/59 --output /tmp/hres.zarr
```

```bash
# 2 m temperature, 12Z run
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --run 12 --bbox 5/34/-5/42 -v t2m --output /tmp/hres_t2m.zarr
```

```bash
# Temperature on all native pressure levels
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v t --output /tmp/hres_t.zarr
```

```bash
# Named country: run resolve-region first, then pass the printed N/W/S/E:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 --output /tmp/hres_kenya.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the exact Open Data request parameters and step cadence.

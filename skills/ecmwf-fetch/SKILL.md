---
name: ecmwf-fetch
description: "Prefer this for ECMWF S2S / extended-range (46-day, 1.5°). Default source is the dynamical.org catalog (`ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree`) — no credentials. Inject ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY only when falling back to ECDS (pre-2026 inits, ocean, 6-hour max/min, accumulated fluxes, `pv`). Prefer dynamical-fetch for medium-range IFS-ENS / AIFS. Default `-v tp`. Short names (`t2m`, `sst`, `t`), not ARCO `2m_temperature`. Real-time has a 2-day embargo. Writes `tp` as `mm day-1` and temperatures as `degree_Celsius` — do not deaccumulate. Country bbox: resolve-region first."
license: MIT
compatibility: Requires Python 3.12 and uv. Default source is the dynamical.org catalog (no credentials). ECDS fallback requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`) and ECMWF_DATASTORES_URL / ECMWF_DATASTORES_KEY (or a `~/.ecmwfdatastoresrc` file). The URL is `https://ecds.ecmwf.int/api`; the key is the personal token from your ECDS account.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
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
      - name: ECMWF_DATASTORES_URL
        description: ECDS API base URL (https://ecds.ecmwf.int/api). Needed only for the ECDS fallback.
      - name: ECMWF_DATASTORES_KEY
        description: Personal ECDS token. Needed only for the ECDS fallback.
---

# ecmwf-fetch

Retrieves an ECMWF S2S / extended-range ensemble forecast and writes a
weather-skills standard dataset Zarr. Default field is `tp`. The default
source is the dynamical.org Icechunk product
`ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree` (101 members, 1.5°, daily 00 UTC
inits from 2026-01-01). Requests the catalog cannot serve (pre-2026 inits,
ocean, 6-hour max/min, accumulated fluxes, `pv`) fall back to ECDS
`s2s-forecasts`. Output short names and `weather_skills_source=ecmwf-s2s`
are unchanged.

## When to use

Prefer `dynamical-fetch` (`ecmwf-ifs-ens-forecast-15-day-0-25-degree` or
`ecmwf-aifs-ens-forecast`) for medium-range ECMWF — credential-free, 0.25°,
no embargo. This skill is **S2S / ER only**. For the 46-day catalog product
under its native names, `dynamical-fetch --dataset ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree`
also works; this skill is what Kenya / S2S scripts call (`-v tp`).

- A task asks for an ECMWF S2S forecast for a specific init date (real-time
  inits are embargoed for 2 days).
- A downstream skill needs the forecast as a weather-skills standard dataset
  Zarr with S2S short names (`tp`, `t2m`, …).

Not for reanalysis, climatology, or deterministic HRES.

## Credentials

The catalog path needs none. Inject ECDS secrets only on the **first**
invocation that falls back to ECDS:

- `ECMWF_DATASTORES_URL` — `https://ecds.ecmwf.int/api`
- `ECMWF_DATASTORES_KEY` — personal ECDS token

Do not call the skill once to discover they are missing, then retry.
`--probe-latest` does not need credentials. Never print, log, or echo the values.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] --output <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--date` — forecast init date. Absolute ISO date `YYYY-MM-DD`. Calendar day: `resolve-time latest`. Latest published init (2-day embargo): `--probe-latest`. Real-time
  ECMWF S2S has run **daily** (00 UTC) since IFS Cycle 48r1 (2023-06-27);
  before that it was Mondays and Thursdays only. The dynamical product starts
  at 2026-01-01. Requesting a date with no published init exits non-zero with
  a clear "no data for this init" message. Recent ECMWF S2S real-time data is
  access-restricted (embargoed) for **2 days**. Transport and auth failures
  are surfaced as clear errors — not raw tracebacks.
- `--probe-latest` — print the latest init on the catalog (`YYYY-MM-DD`) on stdout and exit. No `-o`. Falls back to a 2-day calendar estimate if the catalog is unreachable.
- `--bbox` — required; `N/W/S/E` decimal degrees. The retrieval area (smaller bbox = faster retrieval). To fetch over a country, get its bbox from the `resolve-region` skill and pass the value here.
- `--variable`, `-v` — S2S fields to retrieve. Default `tp`. Pass several in
  one call: `-v tp t2m`, `-v tp -v t2m`, or `-v tp,t2m`. Unknown names exit
  non-zero and print `Available (most used first):`. Use the short names
  (`sst`, `t2m`) — not ARCO `2m_temperature` / `total_precipitation`. ECDS
  form names (`sea_surface_temperature`, `2_m_temperature`) are also accepted.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Variables (most used first)

| `-v` | Field | Notes |
|---|---|---|
| `tp` | Total precipitation | **Default.** Written as a per-step rate (`mm day-1`). Aggregate then `convert-to-totals` for period `mm`. |
| `t2m` | 2 m temperature | Daily mean, `degree_Celsius`. Prefer this for "how warm". |
| `sst` | Sea-surface temperature | Daily mean, `degree_Celsius`. Catalog name is `sea_surface_temperature`; this skill still writes `sst`. S2S GRIB short name `wtmp` is accepted. |
| `d2m` | 2 m dewpoint temperature | Daily mean. |
| `mx2t6` / `mn2t6` | Max / min 2 m temperature in the last 6 hours | ECDS only (catalog has 24 h max/min, not mapped). |
| `u10` / `v10` | 10 m wind components | Instantaneous 00 UTC. |
| `msl` | Mean sea-level pressure | |
| `cape` | Convective available potential energy | Daily mean. |
| `tcw` | Total column water | Daily mean. |
| `gh` | Geopotential height | Pressure levels 1000–10 hPa. Catalog: full 101-member ensemble. ECDS: control only. |
| `t` | Temperature on pressure levels | Same levels. `degree_Celsius`. |
| `u` / `v` / `w` | Wind / vertical velocity on pressure levels | |
| `q` | Specific humidity | Catalog: 10 levels (NaN above 200 hPa). ECDS: 7 levels, control only. |
| `pv` | Potential vorticity | 320 K isentropic level. ECDS only. |

The skill also accepts the rest of the S2S single-level and ocean parameters
(soil moisture/temperature, snow, fluxes, runoff, sea ice, ocean currents,
…). Pass an unknown `-v` to print the full `Available:` list, or see
[references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md).
Ocean fields are ECDS-only (1.0° grid); atmosphere fields are 1.5° — do not
mix ocean with atmosphere in one call.

Catalog surface fields that are 24-hour statistics are all-NaN at lead 0
(no preceding day). This skill drops that empty lead and shifts remaining
steps so `step = 0` is the first 24 h — the same convention as the ECDS
`tp` deaccumulation. Instant fields (10 m wind, MSL/surface pressure,
pressure-level) keep lead 0.

### Output

A Zarr store with the selected data variables and dims `(number, step, latitude, longitude)` — plus `vertical` when a pressure-level or `pv` field is selected. `number=0` is the control; `number=1..100` are perturbed members. `step` is daily (00Z). `tp` is a precipitation **rate** (`mm day-1`) left-labeled so `step = 0` is the first 24h (`[init, init+1d)`); known temperature fields (`t2m`, `sst`, `t`, …) are `degree_Celsius`. Stamped with `weather_skills_source=ecmwf-s2s`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records the run's flag values under underscored names (e.g. a flag
`--time-dim` is recorded as `time_dim`); `version` is the value printed by
`--help`. Inspect a written output's provenance with the `provenance` skill.

## Examples

```bash
# Default: total precipitation, continental Africa (custom bbox — not a country lookup)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 23/-20/-37/59 --output /tmp/ecmwf.zarr
```

```bash
# Several surface fields in one call
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 \
    -v tp t2m --output /tmp/ecmwf_tp_t2m.zarr
```

```bash
# 2 m temperature (the other most-used S2S field)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v t2m --output /tmp/ecmwf_t2m.zarr
```

```bash
# Sea-surface temperature
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v sst --output /tmp/ecmwf_sst.zarr
```

```bash
# Temperature on all native pressure levels
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v t --output /tmp/ecmwf_t.zarr
```

```bash
# Specific humidity + zonal wind. Compose with zonal-moisture-transport for eastward IVT (`viwve`).
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 \
    -v q -v u --output /tmp/ecmwf_q_u.zarr
```

```bash
# Named country: run resolve-region first, then pass the printed N/W/S/E:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 --output /tmp/ecmwf_kenya.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the catalog mapping and the ECDS fallback request parameters.

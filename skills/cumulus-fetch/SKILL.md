---
name: cumulus-fetch
description: On the first call (including --probe-latest), inject secret AZURE_STORAGE_SAS_TOKEN as an environment variable — do not run once without it and retry. Fetch a Cumulus AI operational ensemble precipitation forecast from Azure Blob italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9 (30 ensemble members, 24h IMERG-trained total precipitation, 0.25° grid by default) and write a weather-skills standard dataset Zarr. Use when a task needs the Cumulus forecast grid for clipping, aggregation, comparison, or plotting. Default `-v tp`. Fetch writes `tp` as a per-step rate (`mm day-1`) — do not run deaccumulate after this skill.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private Zarr stores from Azure Blob italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/total_precipitation_24h_acc_imerg_0p25 via adlfs. Requires AZURE_STORAGE_SAS_TOKEN (a read SAS with list and read, sp=rl).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
  openclaw:
    requires:
      env:
        - AZURE_STORAGE_SAS_TOKEN
    primaryEnv: AZURE_STORAGE_SAS_TOKEN
    envVars:
      - name: AZURE_STORAGE_SAS_TOKEN
        description: Read SAS (list and read, sp=rl) for Azure container italynorthdata/data
---

# cumulus-fetch

Opens the Cumulus AI operational ensemble from Azure Blob Storage (one Zarr
store per init, all leads), maps it onto the weather-skills standard dataset,
and writes a local Zarr.

Layout:

```
az://italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/
  total_precipitation_24h_acc_imerg_0p25/YYYY-MM-DD-HH.zarr   # default: 0.25°
  total_precipitation_24h_acc_imerg/YYYY-MM-DD-HH.zarr        # native 1° (--dataset precip_1deg)
  total_precipitation_24h_acc_era5/YYYY-MM-DD-HH.zarr         # ERA5-trained head (--dataset precip_era5)
```

Each store is one init (`HH` = init hour) with dims `(init_time, lead_time,
ensemble_member, latitude, longitude)`: 46 daily leads (24h … 1104h, each the
24h total ending at `valid_time`) and 30 exchangeable members (0..29; member 0
is **not** a control). The default product is a 0.25° global grid
(720×1440). The same run also publishes the native
1° grid (180×360) under a separate variable folder — pass `--dataset precip_1deg` for
that instead.

By default the skill takes the **most recent** init date. Pass `--date` to
pin an init.

## When to use

- A task needs the Cumulus AI operational precipitation ensemble (IMERG-trained
  24h totals) published under the Aurora S2S run on `italynorthdata`.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills standard dataset Zarr.

Not in the dynamical.org catalog — this is the source-specific fetcher.
Prefer `dynamical-fetch` for GFS / GEFS / IFS-ENS / IMERG observations, and
`ecmwf-fetch` for ECMWF S2S.

## Credentials

The container is private. On the **first** invocation, including
`--probe-latest`, inject `AZURE_STORAGE_SAS_TOKEN` (a SAS query string for
`italynorthdata` / `data`, with list and read — `sp=rl`). A leading `?` or a
full blob URL is accepted; only the query string is used. Do not call the
skill once to discover the token is missing, then retry. Never print, log, or
echo the value.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v VAR] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments

- `--dataset` — `precip` (default, the 0.25°
  `total_precipitation_24h_acc_imerg_0p25`), `precip_1deg` (native 1°
  `total_precipitation_24h_acc_imerg`), or `precip_era5` (ERA5-trained
  `total_precipitation_24h_acc_era5`). One of those three folder names also
  works; anything else is an error (exit 2).
- `--date` — forecast init date `YYYY-MM-DD` (the latest `HH` store that day).
  Default: latest published init.
  Calendar day: `resolve-time latest`. Latest published init: `--probe-latest`.
- `--probe-latest [dataset-id]` — print the latest init `YYYY-MM-DD` on stdout
  and exit. No `-o`.
- `--bbox` — optional spatial subset `N/W/S/E`. Native longitude is 0..359;
  negative west/east values still work. Omit for the full 0.25° global grid
  (~5.5 GB per init; the `precip_1deg` alternative is ~350 MB per init).
  Named places: compose with `resolve-region`.
- `--variable`, `-v` — optional; each store holds one field, always written as
  `tp`. Any other name is an error. Accepted: `tp`, `precip`, `precipitation`,
  `total_precipitation`, `total_precipitation_24h_acc_imerg_0p25`,
  `total_precipitation_24h_acc_imerg`, `total_precipitation_24h_acc_era5`.
- `--workers` — max concurrent Zarr chunk requests (default 8, at least 1).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr with dims
`(number, step, latitude, longitude)` and a scalar `time` coord (the init,
from the store's `HH`). `number` is 0..29 (renamed from the store's `ensemble_member`;
exchangeable draws, member 0 is not a control). Source stores are
already 24h period totals — fetch writes `tp` as a per-step **rate**
(`mm day-1`), **left-labeled** so `step = 0` is the first 24h
`[init, init+1d)`. The source already sets negative model output to 0. Do **not**
run `deaccumulate`. Next steps are `aggregate-temporal` then
`convert-to-totals` for period `mm`. Stamped with
`weather_skills_source=cumulus:<variable folder>`.

An init still being written can have all-NaN long leads: they are dropped
(never padded) with a `WARNING` on stderr; an init with no data in the bbox
is an error. Pass `--date` with an earlier init for every lead.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# Latest init, Kenya bbox (dummy; use resolve-region for a real one)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 -v tp -o /tmp/cumulus.zarr
```

```bash
# Pin an init date
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-09-18 --bbox 5/34/-5/42 \
    -o /tmp/cumulus_20260918.zarr
```

```bash
# Weekly totals
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-09-18 --bbox 5/34/-5/42 \
    -o /tmp/cumulus.zarr
uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/cumulus.zarr -o /tmp/cumulus_weekly.zarr --period weekly
uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/cumulus_weekly.zarr -o /tmp/cumulus_weekly_mm.zarr
    -o /tmp/cumulus_weekly.png
```

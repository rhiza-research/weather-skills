---
name: cumulus-fetch
description: Fetch a Cumulus AI operational ensemble precipitation forecast from gs://sheerwater-datalake/cumulus-data (v0.0.1-op perturbed members, 24h IMERG-trained total precipitation) and write a weather-skills standard dataset Zarr. Use when a task needs the Cumulus forecast grid for clipping, aggregation, comparison, or plotting. Requires Google Cloud Application Default Credentials. Default `-v tp`. Fetch writes `tp` as a per-step rate (`mm day-1`) — do not run deaccumulate after this skill.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private NetCDF from gs://sheerwater-datalake/cumulus-data/v0.0.1-op/pf/total_precipitation_24h_acc_imerg/data via gcsfs. Requires Google Cloud Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS or `gcloud auth application-default login`).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
  openclaw:
    requires:
      env:
        - GOOGLE_APPLICATION_CREDENTIALS
    primaryEnv: GOOGLE_APPLICATION_CREDENTIALS
    envVars:
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON with access to gs://sheerwater-datalake
---

# cumulus-fetch

Opens the Cumulus AI operational ensemble from the private Sheerwater
datalake, concatenates one NetCDF per 24h lead, maps it onto the
weather-skills standard dataset, and writes a local Zarr.

Layout:

```
gs://sheerwater-datalake/cumulus-data/v0.0.1-op/pf/
  total_precipitation_24h_acc_imerg/data/YYYY-MM-DD-00-LLLL.nc
```

Each file is one init + one lead (`LLLL` hours, 0024 … 1104). The cube is
a 1° global grid, 29 perturbed members (`number` = 1..29), 46 daily steps.

By default the skill takes the **most recent** init date. Pass `--date` to
pin an init.

## When to use

- A task needs the Cumulus AI operational precipitation ensemble (IMERG-trained
  24h totals) already published in `sheerwater-datalake`.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills standard dataset Zarr.

Not in the dynamical.org catalog — this is the source-specific fetcher.
Prefer `dynamical-fetch` for GFS / GEFS / IFS-ENS / IMERG observations, and
`ecmwf-fetch` for ECMWF S2S.

## Credentials

The bucket is private. On the **first** invocation, including `--probe-latest`,
inject `GOOGLE_APPLICATION_CREDENTIALS` (path to a service-account JSON with
access to `gs://sheerwater-datalake`), or rely on Application Default
Credentials from `gcloud auth application-default login`. Do not call the
skill once to discover they are missing, then retry. Never print, log, or
echo the values.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v VAR] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments

- `--dataset` — product folder under `v0.0.1-op/pf/` (default `precip`, alias
  for `total_precipitation_24h_acc_imerg`).
- `--date` — forecast init date `YYYY-MM-DD`. Default: latest published init.
  Calendar day: `resolve-time latest`. Latest published init: `--probe-latest`.
- `--probe-latest [dataset-id]` — print the latest init `YYYY-MM-DD` on stdout
  and exit. No `-o`.
- `--bbox` — optional spatial subset `N/W/S/E`. Native longitude is 0..359;
  negative west/east values still work. Omit for the full 1° global grid
  (~330 MB per init). Named places: compose with `resolve-region`.
- `--variable`, `-v` — restrict to named data variables (repeatable). Default
  is the product's field, written as `tp`. Aliases: `precip`,
  `total_precipitation`, `total_precipitation_24h_acc_imerg`.
- `--workers` — max concurrent lead-file download threads (default 8).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr with dims
`(number, step, latitude, longitude)` and a scalar `time` coord (the 00 UTC
init). `number` is 1..29 (perturbed members; no control 0). Source files are
already 24h period totals — fetch writes `tp` as a per-step **rate**
(`mm day-1`), **left-labeled** so `step = 0` is the first 24h
`[init, init+1d)`. Clip any small negative source values at zero. Do **not**
run `deaccumulate`. Next steps are `aggregate-temporal` then
`convert-to-totals` for period `mm`. Stamped with
`weather_skills_source=cumulus:<dataset>`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For this fetcher it
is a length-1 array with `skill="cumulus-fetch"` and `input=null`. `args`
records the run's flag values under underscored names; `version` is the value
printed by `--help`. Inspect a written output's provenance with the
`provenance` skill.

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
# Weekly totals then plot
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-09-18 --bbox 5/34/-5/42 \
    -o /tmp/cumulus.zarr
uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/cumulus.zarr -o /tmp/cumulus_weekly.zarr --period weekly
uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/cumulus_weekly.zarr -o /tmp/cumulus_weekly_mm.zarr
# plot lives in the weather-skills-plotting repo
uv run skills/plot/scripts/plot.py -i /tmp/cumulus_weekly_mm.zarr \
    -o /tmp/cumulus_weekly.png \
    --spec '{"inputs":[{"variable":"tp"}]}'
```

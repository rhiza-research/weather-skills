---
name: neuralgcm-fetch
description: Fetch a NeuralGCM S2S ensemble forecast from gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1/<init>/ (Tomorrow Now 2026 realtime) and write a weather-skills standard dataset Zarr. Default `--dataset imerg:precip` (`-v tp`, native `total_precipitation_6hr`); also `era5:surface` (`t2m`, `d2m`). 48 members, 6-hour leads out to 60 days, ~2.8° grid. Requires GCS credentials. Fetch writes `tp` as a per-step rate (`mm day-1`) — do not run deaccumulate after this skill.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private consolidated Zarr from gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1 via gcsfs. Requires Google Cloud Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS or `gcloud auth application-default login`).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - d2m
  openclaw:
    requires:
      env:
        - GOOGLE_APPLICATION_CREDENTIALS
    primaryEnv: GOOGLE_APPLICATION_CREDENTIALS
    envVars:
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON with read access to gs://neuralgcm-s2s
---

# neuralgcm-fetch

Opens a NeuralGCM subseasonal ensemble Zarr from the Tomorrow Now 2026
realtime staging archive, maps it onto the weather-skills standard dataset,
and writes a local Zarr.

Layout:

```
gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1/
  YYYYMMDDTHH/imerg:precip/     # 6-hour precip, IMERG-trained
  YYYYMMDDTHH/era5:surface/     # 2 m temperature and dewpoint
```

Inits are **00 and 12 UTC**. 48 ensemble members, 240 six-hour leads (60
days), ~2.8° global grid (128×64). By default the skill takes the **most
recent** init that has the requested `--dataset`. Pass `--date` to pin a day
(default cycle 00 UTC).

## When to use

- A task needs the Google NeuralGCM S2S precipitation (or 2 m temperature)
  ensemble already published under `gs://neuralgcm-s2s`.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills standard dataset Zarr.

Not in the dynamical.org catalog — this is the source-specific fetcher.
Prefer `dynamical-fetch` for GFS / GEFS / IFS-ENS / IMERG observations, and
`ecmwf-fetch` for ECMWF S2S. This skill is **NeuralGCM S2S only**.

## Credentials

The bucket is private. On the **first** invocation, including `--probe-latest`,
inject `GOOGLE_APPLICATION_CREDENTIALS` (path to a service-account JSON with
access to `gs://neuralgcm-s2s`), or rely on Application Default Credentials
from `gcloud auth application-default login`. Do not call the skill once to
discover they are missing, then retry. Never print, log, or echo the values.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD] [--cycle 00|12] \
    [--dataset imerg:precip|era5:surface] [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Datasets

| `--dataset` | Store under `<init>/` | Output vars |
|---|---|---|
| `imerg:precip` (default; alias `precip`) | `imerg:precip` | `tp` — 6-hour precip amounts converted to a rate |
| `era5:surface` (alias `surface`) | `era5:surface` | `t2m`, `d2m` |

### Arguments

- `--dataset` — product id from the table (default `imerg:precip`).
- `--date` — forecast init date `YYYY-MM-DD`. Default: latest published init.
  Calendar day: `resolve-time latest`. Latest published init: `--probe-latest`.
- `--cycle` — init hour UTC, `00` or `12`. With `--date`, default `00`. When
  `--date` is omitted, the newest cycle that has the product is used.
- `--probe-latest [dataset-id]` — print the latest init `YYYY-MM-DD` on stdout
  and exit. No `-o`.
- `--bbox` — optional spatial subset `N/W/S/E`. Native longitude is 0..360;
  negative west/east values still work. Omit for the full 2.8° global grid.
  Named places: compose with `resolve-region`.
- `--variable`, `-v` — restrict to named data variables (repeatable). Default
  is every field in the product. Precip aliases: `tp`, `precip`,
  `total_precipitation`, `total_precipitation_6hr`. Temperature aliases:
  `t2m` / `2m_temperature`, `d2m` / `2m_dewpoint_temperature`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr with dims
`(number, step, latitude, longitude)` and a scalar `time` coord (the init,
including 12 UTC when `--cycle 12`). `number` is 0..47. Source precip is a
6-hour period total in metres — fetch writes `tp` as a per-step **rate**
(`mm day-1`), **left-labeled** so `step = 0` is the first 6h
`[init, init+6h)`. Known temperatures are `degree_Celsius`. Clip any small
negative precip at zero. Do **not** run `deaccumulate`. For daily/weekly
`mm`, `aggregate-temporal` then `convert-to-totals`. Stamped with
`weather_skills_source=neuralgcm:<dataset>:<stamp>`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For this fetcher it
is a length-1 array with `skill="neuralgcm-fetch"` and `input=null`. `args`
records the run's flag values under underscored names; `version` is the value
printed by `--help`. Inspect a written output's provenance with the
`provenance` skill.

## Examples

```bash
# The 2026-09-11 00 UTC IMERG precip ensemble, Kenya bbox (dummy; use resolve-region)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-09-11 --bbox 5/34/-5/42 \
    -v tp -o /tmp/neuralgcm.zarr
```

```bash
# Latest init, 2 m temperature
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset era5:surface -v t2m \
    --bbox 5/34/-5/42 -o /tmp/neuralgcm_t2m.zarr
```

```bash
# Weekly totals then plot
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-09-11 --bbox 5/34/-5/42 \
    -o /tmp/neuralgcm.zarr
uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/neuralgcm.zarr -o /tmp/neuralgcm_weekly.zarr --period weekly
uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/neuralgcm_weekly.zarr -o /tmp/neuralgcm_weekly_mm.zarr
uv run skills/plot/scripts/plot.py -i /tmp/neuralgcm_weekly_mm.zarr -v tp \
    -o /tmp/neuralgcm_weekly.png
```

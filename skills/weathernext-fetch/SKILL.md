---
name: weathernext-fetch
description: Fetch a Google WeatherNext forecast and write a weather-skills standard dataset Zarr. `--version 2` (default) reads gs://weathernext/weathernext_2_0_0 (64 members, 6-hour leads to 15 days, 0.25deg). `--version 3` reads gs://weathernext3_spatial (64 members, hourly leads to 15 days, 0.1deg surface; requester pays, needs --billing-project) or, with `--product statistics`, the free gs://weathernext3_statistics_spatial ensemble mean and p10-p90. Realtime per-init archives only. Default selects every variable and store chunks span the full globe, so pass -v, --member (or --statistic), and --max-lead; --bbox alone does not shrink the download. `-v tp` is written as a per-step rate (`mm day-1`), so do not run deaccumulate after this skill. Surface `t2m`, `u10`, `v10`, `u100`, `v100`, `msl`, `sst` (v3 adds `d2m`, cloud, `ssrd`, `z500`); pressure-level `t`, `u`, `v`, `w`, `q`, `z`. v3 output uses v2's variable names and dims. Requires GCS credentials.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads consolidated Zarr (v2 store format for WeatherNext 2, v3 for WeatherNext 3) via gcsfs. Requires Google Cloud Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS or `gcloud auth application-default login`). WeatherNext 3 ensemble reads are requester pays and need a billing project (--billing-project or GOOGLE_CLOUD_PROJECT).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
    - 2m_temperature
    - 2m_dewpoint_temperature
    - 10m_u_component_of_wind
    - 10m_v_component_of_wind
    - 100m_u_component_of_wind
    - 100m_v_component_of_wind
    - mean_sea_level_pressure
    - sea_surface_temperature
    - total_cloud_cover
    - surface_solar_radiation_downwards_1hr
    - specific_humidity
    - temperature
    - u_component_of_wind
    - v_component_of_wind
    - vertical_velocity
    - geopotential
  openclaw:
    requires:
      env:
        - GOOGLE_APPLICATION_CREDENTIALS
    primaryEnv: GOOGLE_APPLICATION_CREDENTIALS
    envVars:
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON (any Google Cloud identity can read the WeatherNext buckets)
      - name: GOOGLE_CLOUD_PROJECT
        description: Billing project for requester-pays WeatherNext 3 ensemble reads (alternative to --billing-project)
---

# weathernext-fetch

Opens a Google WeatherNext forecast Zarr from a realtime archive, maps it onto
the weather-skills standard dataset, and writes a local Zarr. Pick the model
generation with `--version` (default `2`).

| `--version` / `--product` | Store | Members | Leads | Grid | Cost |
|---|---|---|---|---|---|
| `2` | `gs://weathernext/weathernext_2_0_0/zarr/2025_to_present/` | 64 | 60 x 6h (15 days) | 0.25° | free (any GCP identity) |
| `3` (`--product ensemble`) | `gs://weathernext3_spatial/weathernext_3_0_0/zarr/2026_to_present/` | 64 | 360 x 1h (15 days) | 0.1° surface; 0.25° upper air (6-hourly); 0.05° station-head | **requester pays** |
| `3 --product statistics` | `gs://weathernext3_statistics_spatial/weathernext_3_0_0_statistics/zarr/2026_to_present/` | — (mean, p10/p25/p50/p75/p90) | 360 x 1h | 0.1° / 0.05° surface only | free |

Every archive has one store per init:

```
<archive>/YYYYMMDD_HHhr_01_preds/predictions.zarr/
```

Inits are **00, 06, 12, and 18 UTC** (15-day forecasts). WeatherNext 3 also
publishes **48-hour interim inits** at every other hour (01–05, 07–11, ...);
reach them with an explicit `--cycle`. By default the skill takes the **most
recent 00/06/12/18** init. Pass `--date` to pin a day (default cycle 00 UTC).
WeatherNext 2 inits go back to 2025-01-01; WeatherNext 3 realtime starts in 2026.

**Scope:** only the realtime per-init archives above. The yearly WeatherNext 2
stores (`2022_to_2023` ... `2024_to_2025`) and WeatherNext 3 backfill stores
(`6hr_inits.zarr` / `interim_1hr_inits.zarr`) use a multi-init layout and are
out of scope.

## When to use

- A task needs a Google WeatherNext forecast — v2 (GenCast-lineage, 0.25°,
  6-hourly) or v3 (hourly, 0.1°, satellite-initialized) — for precipitation,
  temperature, wind, pressure, cloud, radiation, humidity, or geopotential.
- A downstream skill will clip, aggregate, compare, or plot the result as a
  weather-skills standard dataset Zarr, e.g. against `arco-era5-fetch` (same
  ERA5-lineage variable names and units) or `chirps-fetch`/`imerg-fetch` for
  precip verification.
- Use `--version 3 --product statistics` when the ensemble mean or spread
  percentiles are enough — it is free, and one variable-statistic is ~64x
  smaller than the full ensemble.

Not in the dynamical.org catalog — this is the source-specific fetcher.
Prefer `ecmwf-fetch` for ECMWF S2S, `neuralgcm-fetch` for NeuralGCM S2S. This
skill is **Google WeatherNext 2 and 3 only**.

## Credentials

All buckets require an authenticated Google Cloud identity (not
anonymous-public, but no special allowlist). On the **first** invocation,
including `--probe-latest`, inject `GOOGLE_APPLICATION_CREDENTIALS` (path to a
service-account JSON), or rely on Application Default Credentials from
`gcloud auth application-default login`. Do not call the skill once to
discover they are missing, then retry. Never print, log, or echo the values.

The **WeatherNext 3 ensemble** bucket is **requester pays**: every listing and
read is billed to the project in `--billing-project` (default
`$GOOGLE_CLOUD_PROJECT`, then `$CLOUDSDK_CORE_PROJECT`). Without one the skill
exits 2 before touching GCS. Google recommends compute in `us-east1` to avoid
egress charges. The v2 and v3-statistics buckets need no billing project.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 2|3] [--product ensemble|statistics] \
    [--date YYYY-MM-DD] [--cycle HH] [--bbox N/W/S/E] [-v VAR ...] [--member N ...] \
    [--statistic mean|p10|p25|p50|p75|p90 ...] [--max-lead HOURS] [--billing-project PROJECT] \
    -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 3 ...] --probe-latest
```

### Size warning

**Store chunks span the full global grid** (one chunk per member per lead, or
per member per 6-hour lead block in v3), so `--bbox` alone does **not** reduce
bytes transferred — it only shrinks the written output. To cut the download,
pass `-v` (fewer variables), `--member` (fewer of the 64 members) or, for v3
statistics, `--statistic`, and `--max-lead` (fewer leads). All are applied
before any chunk is read. Reads are bandwidth-bound: each 0.1° v3 field is
~26 MB per lead, so e.g. 2 fields x 6 leads is ~300 MB.

- v2: ~1 TB full cube; one surface variable, global, all members, ~16 GB.
- v3 ensemble: one 0.1° surface variable, one member, all 360 leads is ~9 GB
  (~600 GB for all 64 members) — and it is billed egress.
- v3 statistics: one variable-statistic, all 360 leads, ~9 GB.

For leads that don't start at init, cap with `--max-lead`, then use the
generic `select` skill (`--dim step`) on the result.

### Arguments

- `--version` — `2` (default) or `3`.
- `--product` — v3 only. `ensemble` (default, 64 members, requester pays) or
  `statistics` (precomputed ensemble mean and percentiles, free).
- `--date` — forecast init date `YYYY-MM-DD`. Default: latest published
  00/06/12/18 init. Calendar day: `resolve-time latest`. Latest published
  init: `--probe-latest`.
- `--cycle` — init hour UTC. v2: `00`/`06`/`12`/`18`. v3: any hour `00`–`23`
  (non-synoptic hours are 48h interim runs). With `--date`, default `00`.
  When `--date` is omitted, the newest matching init that has a store is used.
- `--probe-latest` — print the latest init `YYYY-MM-DD` on stdout and exit.
  No `-o`. Honors `--version`, `--product`, and `--cycle`.
- `--bbox` — optional spatial subset `N/W/S/E`. Native longitude is 0..360;
  negative west/east values still work. Omit for the full global grid (see
  size warning). Named places: compose with `resolve-region`.
- `--variable`, `-v` — restrict to named data variables (repeatable). Short
  aliases: `tp`/`precip`, `t2m`, `d2m`, `u10`, `v10`, `u100`, `v100`, `msl`,
  `sst`; pressure level `t`, `u`, `v`, `w`, `q`, `z`/`geopotential`. v3 adds
  `tcc`, `lcc`, `mcc`, `hcc`, `ssrd`, `si10`/`si100` (statistics only),
  `z300`, `z500`, `t300`, `t500`, `u1000`, `v1000`, `imerg_tp`,
  `experimental_tp`, and the 0.05° `station_head_temperature_2m` /
  `station_head_dewpoint_temperature_2m`. Native long names also work (v2
  `total_precipitation_6hr`, v3 `temperature_2m`, `total_precipitation_1hr`,
  ...). Default: v2 every field; v3 every 0.1° surface field.
  **v3 stores fields on different grids and lead axes** — 0.1° hourly
  surface, 0.05° hourly station-head, 0.25° hourly single-level upper air
  (`z500`, `t500`, ...), and 0.25° **6-hourly** pressure-level cubes (`t`, `u`,
  `v`, `w`, `q`, `z`). One run can only mix fields from one group; mixing
  groups exits 2 with the grouping spelled out.
- `--member` — restrict to these 0-based ensemble member indices (repeatable,
  e.g. `--member 0`). Default: all 64. Applied before download. Not valid with
  `--product statistics`.
- `--statistic` — v3 statistics only: restrict to these statistics
  (repeatable). Default: all six.
- `--max-lead` — keep only leads up to this many hours after init (e.g.
  `--max-lead 48`). Cuts the download proportionally.
- `--billing-project` — v3 ensemble only; see **Credentials**.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr with dims
`(number, step, lat, lon)` and a scalar `time` coord (the init), plus
`level` for pressure-level fields. `number` is 0..63 (absent for v3
statistics). `valid_time` (on `step`) carries each lead's wall-clock
datetime. v3's native `lead_time` x `lead_subtime` is flattened into one
hourly `step`, and the native `lat_0p1`/`lon_0p1` (etc.) become `lat`/`lon`.

Source precip (v2 `total_precipitation_6hr`, v3 `total_precipitation_1hr`) is
a **period total** in metres — fetch writes it as `tp`, a per-step **rate**
(`mm day-1`), **left-labeled** so `step = 0` is the first period
(`[init, init+6h)` in v2, `[init, init+1h)` in v3). Every field's `step` is
shifted the same way; `valid_time` keeps each field's true valid time. Clip
any small negative precip at zero. Do **not** run `deaccumulate` (it is not
cumulative-since-init). For daily/weekly `mm`, `aggregate-temporal` then
`convert-to-totals`.

v3 fields shared with v2 / ARCO-ERA5 take the ERA5-lineage names
(`2m_temperature`, `10m_u_component_of_wind`, ...), so v2 and v3 outputs
line up. v3 statistics variables are `<name>_<stat>` (`tp_mean`,
`2m_temperature_p90`, ...). Air/surface temperatures are converted to
`degree_Celsius`; other variables keep native units (`m s-1`, `Pa`,
`Pa s-1`, `kg kg-1`, `m2 s-2`, `J m-2`, cloud fraction `1`) with CF
`standard_name`s. Stamped with `weather_skills_source` =
`weathernext:<stamp>` (v2), `weathernext3:<stamp>`, or
`weathernext3-statistics:<stamp>`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For this fetcher it
is a length-1 array with `skill="weathernext-fetch"` and `input=null`. `args`
records the run's flag values under underscored names (`--version` is recorded
as `model_version`); `version` is the skill version printed by `--help`.
Inspect a written output's provenance with the `provenance` skill.

## Examples

```bash
# WeatherNext 2, latest init, Kenya bbox, precip + 2m temperature, first 4 members
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 \
    -v tp -v t2m --member 0 --member 1 --member 2 --member 3 \
    -o /tmp/weathernext.zarr
```

```bash
# WeatherNext 3 ensemble (requester pays), one member, hourly precip over Kenya
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --version 3 --billing-project my-gcp-project \
    --bbox 5/34/-5/42 -v tp --member 0 -o /tmp/wn3_tp_m0.zarr
```

```bash
# WeatherNext 3 statistics (free): ensemble mean and p90 2m temperature
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --version 3 --product statistics \
    --bbox 5/34/-5/42 -v t2m --statistic mean --statistic p90 -o /tmp/wn3_t2m_stats.zarr
```

```bash
# A pinned v2 init, one member, 500 hPa geopotential over a bbox (adds a `level` dim)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-09-25 --cycle 12 \
    --bbox 5/34/-5/42 -v geopotential --member 0 -o /tmp/weathernext_z.zarr
uv run skills/select/scripts/select_dim.py -i /tmp/weathernext_z.zarr \
    -o /tmp/weathernext_z500.zarr --dim level --value 500
```

```bash
# Weekly precip totals then plot (single member, for a fast preview)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 \
    -v tp --member 0 -o /tmp/weathernext_m0.zarr
uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/weathernext_m0.zarr -o /tmp/weathernext_weekly.zarr --period weekly
uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/weathernext_weekly.zarr -o /tmp/weathernext_weekly_mm.zarr
uv run skills/plot/scripts/plot.py -i /tmp/weathernext_weekly_mm.zarr \
    -o /tmp/weathernext_weekly.png --spec '{"inputs":[{"variable":"tp"}]}'
```

---
name: openaq-fetch
description: "On the first call (including --probe-latest), inject secret OPENAQ_API_KEY as an environment variable — do not run once without it and retry. Fetch OpenAQ air-quality station observations (PM2.5, PM10, NO2, O3, SO2, CO) for a date range and region, and write a point_obs weather-skills standard dataset Zarr. Use when a task needs in-situ air-quality and atmospheric-composition data, e.g. to compare against gridded model output."
license: MIT
compatibility: Requires Python 3.12 and uv. Uses the OpenAQ v3 REST API over HTTPS; requires a free OPENAQ_API_KEY in the environment (register at https://explore.openaq.org/register).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - pm25
    - pm10
    - no2
    - o3
    - so2
    - co
  openclaw:
    requires:
      env:
        - OPENAQ_API_KEY
    primaryEnv: OPENAQ_API_KEY
    envVars:
      - name: OPENAQ_API_KEY
        description: Free personal token from https://explore.openaq.org/register
---

# openaq-fetch

Downloads ground-based air-quality observations from the OpenAQ v3 API. It finds
monitoring locations inside the requested bounding box, fetches each matching
sensor's daily-aggregated values over the date range concurrently, and writes a
point_obs Zarr store.

## When to use

- A task needs daily air-quality and atmospheric-composition station observations
  (PM2.5, PM10, NO2, O3, SO2, CO) for a region.
- A downstream skill will compare stations against gridded data or
  aggregate them temporally.

## Credentials

The fetch process does not inherit host secrets. On the **first** invocation,
including `--probe-latest`, inject `OPENAQ_API_KEY` (free; register at
https://explore.openaq.org/register). Do not call the skill once to discover
it is missing, then retry. Never print, log, or echo the value.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox N/W/S/E --start-time YYYY-MM-DD --end-time YYYY-MM-DD [-v VAR ...] [--min-coverage F] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--bbox` — spatial subset `N/W/S/E` decimal degrees (required; selects which
  monitoring locations to fetch). N must be >= S and W <= E (OpenAQ bboxes cannot
  cross the antimeridian; split such a region into two calls). To fetch over a
  country, get its bbox from the `resolve-region` skill.
- `--start-time`, `--end-time` — inclusive date range of station-local calendar days
  (see Output). Each value is an absolute ISO date `YYYY-MM-DD`. Calendar windows: `resolve-time last-2w`. Latest published day: `--probe-latest`.
- `--probe-latest` — print the latest UTC date (`YYYY-MM-DD`) any PM2.5 sensor
  reported in the last 7 days on stdout and exit. No `-o`. That day is usually
  still partial; see `--min-coverage`.
- `--variable`, `-v` — restrict to one pollutant; repeat once per variable.
  Choices: `pm25`, `pm10`, `no2`, `o3`, `so2`, `co` (anything else exits 2). Omit
  for all six.
- `--min-coverage` — minimum fraction (0–1) of the 24 hourly values a station-day
  needs for its daily mean to be kept (default 0.75, i.e. 18 h). Days below it are
  left missing (NaN), never filled; use `1.0` for complete days only.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-sensor fetch threads (default 8, >= 1). A
  concurrency knob only, not data: threads overlap response waits only. All
  requests are client-side rate-limited under OpenAQ's published limits (60/minute,
  2,000/hour), with a small margin on the hourly cap, so raising `--workers` does
  not raise the request rate.

### Output

A fully CF-1.13 timeSeries Discrete Sampling Geometry (DSG) Zarr with dims
`(time, station_id)`, coords `latitude(station_id)`, `longitude(station_id)`,
`name(station_id)`, and data variables among `pm25`, `pm10`, `no2`, `o3`, `so2`,
`co` — whichever were requested and present. `station_id` is the OpenAQ location
id; when one location has several sensors for the same pollutant (and unit), their
daily values are averaged into one column.

Daily values come from OpenAQ's `/sensors/{id}/days` aggregates: the mean of the
hourly values over each station's **local** calendar day, so days are not
aligned across time zones. `time` is that local date (at 00:00, no offset). A
day is kept only when at least `--min-coverage` of its 24 hourly values exist;
days below it, days without coverage info, and negative daily means (invalid
readings or -999-style sentinels) are left missing. stderr reports how many
station-days were dropped for each reason and how many kept means are from
partial (< 24 h) days.

CF stamping:
- Global attrs `Conventions="CF-1.13"`, `featureType="timeSeries"`, plus
  `title`/`source`/`institution`/`references`/`history`.
- `station_id` carries `cf_role="timeseries_id"`; lat/lon/time carry their CF
  `standard_name`/`units`/`axis`; the time axis carries udunits reference-time
  units + a calendar in its write encoding.
- Every pollutant variable carries `coordinates="latitude longitude time"` (the
  load-bearing DSG attr), a `long_name`, `cell_methods="time: mean"` (each daily
  value is the within-day mean), a `comment` stating the local-day and
  `--min-coverage` rule, and a `_FillValue` of NaN for the station-time cells
  with no kept value.

Units are forwarded **verbatim** from the OpenAQ API per parameter (µg/m³ for
particulates, ppm/ppb for gases, as the provider reports) — never normalized or
converted — and are validated under udunits at write time; a unit that genuinely
fails to parse halts the run naming the parameter and unit. A CF `standard_name`
is set only where a CF-table entry cleanly matches the reported unit family: a
mass-concentration reading (µg/m³, mg/m³) gets a `mass_concentration_of_*_in_air`
name, a mole-fraction reading (ppm, ppb) a `mole_fraction_of_*_in_air` name.
Where no verified CF name applies the `standard_name` is omitted (units +
`long_name` alone is CF-valid); particulate matter has no mole-fraction CF name,
so `pm25`/`pm10` carry a `standard_name` only when reported as a mass
concentration. The store is stamped with `weather_skills_source=openaq`.

### Dropped sensors

Per-sensor failures are isolated, never silently lost. A sensor whose daily
fetch fails (5xx/timeout/429) is retried once, then dropped with a per-line
stderr note. A sensor that reports a unit differing from the first-seen unit for
its pollutant is dropped too (its column cannot be merged without mislabeling),
as is a sensor with a missing/empty units value (a CF data variable cannot carry
`units=None`). At the end the run prints an aggregate count of all dropped
sensors to stderr so the loss is visible at a glance. The first-seen unit wins, so
which unit a pollutant ends up in depends on the API's location order; units are
never converted.

If a requested `-v` pollutant yields no in-window data — every one of its sensors
returned nothing or was dropped — it is omitted from the output and named in a
stderr warning; the run still succeeds with the variables that do have data.

### Errors

- `OPENAQ_API_KEY` unset, an unknown `-v`, an inverted/antimeridian `--bbox`,
  `--workers` < 1, or `--min-coverage` outside 0–1: `Error:` and exit 2 before any
  network call; the key value is never printed.
- A wrong/expired key (the API returns 401/403), whether on the locations query
  or on a per-sensor daily-values fetch (a key that expires mid-run or lacks
  `/sensors` access): a one-line actionable message and a non-zero exit; the key
  is never echoed. An auth failure is surfaced as fatal rather than counted as a
  routine per-sensor drop.
- HTTP 429 on the locations query that survives the backoff retry: an `Error:`
  naming the rate limit, exit 1.
- No sensors or no observations for the bbox/window: a non-zero exit with a
  clear message.

There is no proactive size guard: `--bbox` is required (a global query would be
unbounded), but within it the caller decides the window. Request rate, however,
is guarded proactively (the client-side rate limiter above); a 429 that still
arrives is retried once after a 60–300 s backoff (its `Retry-After` header when
present, else 60 s), and other transient failures are handled reactively
(retry-once, then drop).

### Memory and performance

This is station data: memory scales with the number of sensors inside `--bbox`
times the window length times the selected pollutants, bounded by the bbox rather
than any global grid. Per-sensor daily series are fetched concurrently and held
only transiently; `--workers` raises concurrency at a modest, bounded memory cost.
On tight-memory hosts, narrow the `--bbox` or shorten the window.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# PM2.5 for NYC-area stations over three days
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 41/-74/40/-73 --start-time 2024-06-01 --end-time 2024-06-03 \
  -v pm25 -o /tmp/openaq.zarr

# NO2 + O3 over a small bbox for one week
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 41/-74/40/-73 --start-time 2024-06-01 --end-time 2024-06-07 \
  -v no2 -v o3 -o /tmp/openaq_gases.zarr
```

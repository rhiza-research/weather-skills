---
name: arco-era5-fetch
description: Fetch ARCO-ERA5 reanalysis (temperature, wind, precipitation, pressure, and more) for a date range and region from the public, credential-free Google Cloud Zarr store, and write a weather-skills standard dataset Zarr. Use when a task needs multi-variable gridded reanalysis ground truth for comparison, verification, or downstream clipping/aggregation/plotting. Data lags real time by ~6 days (ERA5T); run --probe-latest and end the window there — hours past the data end are dropped with a stderr warning.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public ARCO-ERA5 analysis-ready Zarr from Google Cloud (gs://gcp-public-data-arco-era5) over anonymous access; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - 2m_temperature
    - total_precipitation
    - 2m_dewpoint_temperature
    - 10m_u_component_of_wind
    - 10m_v_component_of_wind
---

# arco-era5-fetch

Opens the [ARCO-ERA5](https://github.com/google-research/arco-era5) analysis-ready
Zarr store (`gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3`),
subsets it by bounding box, time range, and variables, maps it onto the weather-skills
standard dataset analysis shape, and writes a consolidated Zarr store. The store is a
uniform 0.25° equiangular lat/lon grid, hourly, opened with anonymous Google
Cloud access — no credentials and no API queue.

## When to use

- A task needs multi-variable reanalysis (2m temperature, winds, precipitation,
  geopotential, pressure-level fields) as gridded observations/ground truth.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  standard dataset Zarr.

Not a forecast — ERA5 is reanalysis. For forecast grids prefer
`dynamical-fetch`; use `ecmwf-fetch` only for ECMWF S2S.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`. Calendar windows: `resolve-time last-2w --as-of <probe-latest>`. Latest published day: `--probe-latest`. If the window runs past the data end, the all-NaN trailing hours are dropped (never filled) and stderr prints the effective end; a window with no data at all exits non-zero.
- `--probe-latest` — print the last day with data (`YYYY-MM-DD`) on stdout and exit. No `-o`. The store pre-allocates its time axis through 2050, so this reads the store's ERA5T stop date and checks one `2m_temperature` point at 23 UTC (stepping back if empty). Typical lag is ~6 days. stderr notes that days after the final-ERA5 stop (~2-3 months back) are preliminary ERA5T.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. Longitudes are normalized
  to the [-180, 180) convention, so negative west/east values select correctly on
  ERA5's native 0..360 grid. The slice follows each axis's own order, so any
  region works. A bbox whose west is greater than its east is read as crossing
  the antimeridian (e.g. `10/170/-10/-170`): the cells with `lon >= west` or
  `lon <= east` are kept and the interior band is dropped, preserving the grid's
  native ascending longitude order. The two dateline-flanking spans (the cells
  near +180 followed by the cells near -180) stay in ascending order — monotonic,
  though not physically contiguous across the dateline — so downstream tools that
  assume a sorted longitude keep working.
  Omit to fetch the full native grid. To fetch over a country, get its bbox from
  the `resolve-region` skill.
- `--variable`, `-v` — restrict to one data variable; repeat once per variable
  (`-v 2m_temperature -v total_precipitation`). Almost always supply at least one:
  ARCO-ERA5 carries hundreds of surface and pressure-level fields, and omitting
  `--variable` selects all of them. An unknown name exits 2 and prints the
  available variables.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated, fully CF-1.13 compliant weather-skills standard dataset analysis Zarr with a
`time` dimension and dims `(time, latitude, longitude)` — plus `level` when a
pressure-level variable is selected. The weather-skills standard dataset is a CF superset: the
output passes CF first, then carries the `weather_skills_history` provenance key.

CF stamping on the output:

- **Global attrs:** `Conventions="CF-1.13"`, plus `title`, `institution`,
  `source` (the ARCO-ERA5 store path), `references`, and `history`.
- **Coordinates:** `latitude` (`standard_name=latitude`, `units=degrees_north`,
  `axis=Y`), `longitude` (`standard_name=longitude`, `units=degrees_east`,
  `axis=X`), `time` (`standard_name=time`, `axis=T`, with CF `units` and
  `calendar=proleptic_gregorian` carried in the on-disk encoding). When a
  pressure-level variable is selected, `level` carries `standard_name=air_pressure`,
  `units=hPa`, and `positive=down`.
- **Data variables:** every variable carries a udunits-valid `units` and a
  `long_name`. Air temperatures (`2m_temperature`, pressure-level
  `temperature`) become `degree_Celsius`. The hourly precip accumulations
  (`total_precipitation`, `convective_precipitation`,
  `large_scale_precipitation`, metres per hour) become `mm day-1` rates
  (× 1000 × 24). ERA5 labels each accumulation at the END of its hour; the
  output relabels it at the start, so a day's 00..23 UTC rows are that day's
  24 hours (store hours 01..24). The last hour of the latest published day is
  therefore NaN for precip (its accumulation is not out yet). Every variable is
  stamped `data_interval="1 hour"`. Other accumulated or mean-rate fields
  (radiation in `J m**-2`, `evaporation`, `runoff`, `snowfall`, `mean_*_rate`)
  are not converted or relabelled and keep ERA5's end-of-hour label; remaining variables
  keep the ARCO store's `units`/`long_name`/`standard_name`. A few udunits-invalid
  unit placeholders the store uses
  (`(0 - 1)`, `~`, `dimensionless`, `m of water equivalent`) are normalized to a
  CF-valid spelling. Each variable's final `units` string is then validated
  against udunits: a variable whose source units are missing, or are
  non-parseable and not covered by that normalization map, is a hard failure —
  the fetcher exits non-zero naming the variable and its offending units rather
  than writing invalid units under a `Conventions="CF-1.13"` claim. A
  missing-units variable is never silently relabeled dimensionless.
  `standard_name` is set when the store supplies one or a curated value applies,
  and omitted otherwise (CF permits this).

Stamped with `weather_skills_source=arco-era5`.

### Memory, performance, and error handling

`--variable` and the window length set how much is downloaded: store chunks
are one hour × the full globe per variable (~4 MB for a surface field, ~150 MB
for a 37-level pressure field), so every selected hour downloads whole global
chunks and `--bbox` only shrinks what is kept, not what is read. An unselected
variable is never read. Measured: one day of two surface variables over Kenya
peaks at ~230 MB RSS (flat for three days); one day of pressure-level
`temperature` downloads ~3.7 GB and peaks at ~1.2 GB RSS.

The selection is materialized into host memory in a single eager load, so a
very large request (no `--bbox`, all variables, a pressure-level
variable spanning all 37 levels, or a long window) is slow and may exhaust
memory. The fetcher does not refuse a request on a size estimate — what to fetch
is your call. Keep a request affordable by selecting the specific variables you
need, keeping `--bbox` tight, and keeping the window to days or a small number of
weeks; run `clip-region` afterward to shrink further.

Errors are reported reactively. Store-open, network, and path failures emit a
one-line actionable message instead of a raw traceback. If the eager load
actually runs out of memory and raises a catchable `MemoryError`, the fetcher
reports it and suggests narrowing with `-v`, `--bbox`, or a shorter window (under
Linux memory overcommit an OOM may instead kill the process outright, printing
only "Killed").

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# 2m temperature over a country for two days (dummy bbox; use resolve-region for a real one)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-01-01 --end-time 2026-01-02 \
  --bbox 5/34/-5/42 -v 2m_temperature -o /tmp/arco.zarr

# One week, two variables, continental Africa (custom bbox)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-05-24 --end-time 2026-05-30 \
  --bbox 23/-20/-37/59 -v 2m_temperature -v total_precipitation -o /tmp/arco_week.zarr

# A pressure-level variable for one day — adds a CF `level` (air_pressure) dim
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-01-01 --end-time 2026-01-01 \
  --bbox 5/34/-5/42 -v temperature -o /tmp/arco_level.zarr
```

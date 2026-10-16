---
name: oisst-fetch
description: Fetch NOAA OISST v2.1 daily sea-surface temperature for a date range and region from NOAA PSL's public OPeNDAP server, and write a weather-skills standard dataset Zarr. Use when a task needs credential-free gridded SST observations, e.g. for ocean analysis or comparison against forecasts/reanalysis.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads NOAA OISST v2.1 from NOAA PSL's OPeNDAP server (psl.noaa.gov) over HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - sst
---

# oisst-fetch

Reads NOAA's Optimum Interpolation Sea Surface Temperature (OISST) v2.1 daily
0.25° global analysis from NOAA PSL's public OPeNDAP server, subsets it by
bounding box and time range, maps it onto the weather-skills standard dataset analysis shape, and
writes a consolidated Zarr store. OPeNDAP lets the skill pull only the requested
window rather than whole yearly files, with no credentials.

## When to use

- A task needs gridded sea-surface temperature observations (daily, 0.25°,
  global, 1981-09 to present), without credentials.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  standard dataset Zarr.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date
  `YYYY-MM-DD`. Calendar windows: `resolve-time last-2w`. Latest published day: `--probe-latest`.
- `--probe-latest` — print the latest available `YYYY-MM-DD` on stdout and exit. No `-o`;
  takes no IDENT (exit 2). The latest days are preliminary (see Output).
- `--bbox` — spatial subset `N/W/S/E` decimal degrees, either longitude convention.
  West > east crosses the antimeridian (e.g. `2/178/-2/-178`). The box is read as
  contiguous slices of OISST's native 0..360 grid, so boxes crossing 0° are as
  cheap as any other. Omit for the full global grid. To fetch over a region, get
  its bbox from the `resolve-region` skill.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)`, carrying `sst` (sea-surface temperature, °C;
`data_interval="1 day"`). Grid: 0.25° cell centres (…, -0.125, 0.125, …), longitude
ascending in [-180, 180). Land cells are NaN. Stamped with
`weather_skills_source=oisst`.

- **Preliminary days**: NOAA revises days less than ~15 days old. PSL serves them
  in the same yearly file with no per-day flag, so neither the output nor
  `--probe-latest` can mark them. A window ending in the last 15 days (and every
  probe) prints a stderr note.
- **Missing days are never filled**: days before 1981-09-01, past the latest
  published day, or in a year file that is not published yet are left out, and
  stderr names how many requested days are missing and their span.

The store is fully **CF-1.13** compliant (the weather-skills standard dataset is a CF superset —
CF plus the `weather_skills_history` provenance key):

- Global attrs: `Conventions="CF-1.13"`, `title`, `source` (NOAA OISST v2.1, read
  over NOAA PSL OPeNDAP), `institution`, `references`, `history`.
- `latitude`/`longitude`: `standard_name`, `units` (`degrees_north`/
  `degrees_east`), `axis` (`Y`/`X`).
- `time`: `standard_name=time`, `axis=T`, with an explicit udunits `units` and
  `calendar` in the write encoding.
- `sst`: `standard_name=sea_surface_temperature`, `long_name`, `units`
  (`degree_Celsius`), and an explicit NaN `_FillValue` in
  the encoding for land cells.

cf-xarray resolves the X/Y/T axes on the output, and the write is gated on that
decode succeeding.

### Memory and performance

There is one variable (`sst`); `--bbox` and the window length are the memory
levers. OPeNDAP transfers only the bbox and window (one request per year, two
for a box crossing 0°), but every year's selection is held in memory until the
single write at the end. The global grid is ~720×1440, about 4 MB per day as
float32 (~1.5 GB per global year). Measured peak RSS: ~115 MB for a week over a
20°×20° box, ~145 MB for one global day. Keep `--bbox` tight and the window short.

### Errors

OISST is served over NOAA PSL's OPeNDAP server, which limits request size. The
skill reports failures reactively — it does not pre-estimate request size or
refuse against any threshold — so the messages below appear only after the
provider or the resolver fails:

- **Transport / oversized request** — the data transfer is rejected at load time
  (a DAP failure, typically a large `--bbox` over a long window). The message
  names the remedy: reduce `--bbox` and/or shorten the date range; this is not a
  credentials or availability problem, so retrying the same request will not help.
- **Transient server error** — NOAA PSL returns a 502 / proxy HTML page or an
  intermittent `NetCDF: Access failure` instead of OPeNDAP data. The skill
  retries once after 2 s, then exits. `--probe-latest` treats a
  non-datetime `time` axis (the usual symptom of an HTML error page that
  netCDF4 did not raise on) as this case rather than crashing. Distinct from
  the oversized-request case; retrying later may succeed.
- **Availability** — a past year file does not exist (before 1981); check the
  date range. A current- or next-year file that is not published yet is skipped,
  not an error.
- **Empty window** — the requested window is in range but contains no data; exits
  with a "no data in window" message, kept distinct from a failure.

Exit codes: exit `2` covers argument errors (a malformed date, a reversed
range, a malformed `--bbox`, an IDENT on `--probe-latest`) and the transport /
oversized or unrecognized read failures. Exit `1` covers the transient,
availability, and empty-window cases, a `--bbox` that selects no grid cells, and
a CF stamping or decode failure. Nothing is written until every year has loaded,
so a failure never leaves a partial store.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# SST over a coastal bbox for three days (dummy bbox; use resolve-region for a real one)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 --start-time 2024-06-01 --end-time 2024-06-03 \
  -o /tmp/oisst.zarr

# Three weeks over a bounded region
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 --start-time 2024-05-12 --end-time 2024-06-01 \
  -o /tmp/oisst_week.zarr
```

---
name: cmip6-fetch
description: Fetch a CMIP6 climate-model projection (e.g. temperature, precipitation) for a date range and region from the public, credential-free Pangeo Google Cloud catalog, and write a weather-skills standard dataset Zarr. Use when a task needs climate-projection grids (historical or future scenario) for downstream clipping, aggregation, comparison, or plotting.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public Pangeo CMIP6 collection from Google Cloud (gs://cmip6) over anonymous access; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - tas
    - pr
---

# cmip6-fetch

Resolves a single CMIP6 dataset from the [Pangeo CMIP6](https://pangeo-data.github.io/pangeo-cmip6-cloud/)
catalog on Google Cloud, opens its analysis-ready Zarr store anonymously, subsets
it by bounding box and time, maps its dimensions onto the weather-skills standard dataset analysis
shape, and writes a consolidated Zarr store. CMIP6 is faceted, so the dataset is
selected with facet flags (model, experiment, variable, member, table, grid) that
are validated against the catalog CSV.

## When to use

- A task needs climate-model projection output — historical runs or future
  scenarios (ssp*) — as gridded data, with no credentials.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  standard dataset Zarr.

CMIP6 is model projection, not observation or short-range forecast. Historical
runs cover 1850–2014 and scenario (`ssp*`) runs continue to 2100, so the natural
windows are multi-year to multi-decadal.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model <id> --experiment <id> -v <variable> \
  [--member <id>] [--table <id>] [--grid <label>] \
  --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments

- `--model` — CMIP6 `source_id` (e.g. `GFDL-CM4`).
- `--experiment` — CMIP6 `experiment_id` (e.g. `historical`, `ssp245`).
- `--variable`, `-v` — CMIP6 `variable_id`. CMIP6 stores one variable per
  dataset, so this both selects the dataset and names the output variable
  (e.g. `tas`, `pr`).
- `--member` — CMIP6 `member_id` (default `r1i1p1f1`).
- `--table` — CMIP6 `table_id` fixing the frequency/realm (default `Amon`;
  e.g. `day`).
- `--grid` — CMIP6 `grid_label` (e.g. `gn`, `gr1`). Required only when more than
  one grid matches the other facets; otherwise the single match is used.
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`. Future dates are allowed for scenario experiments (which run to
  2100). Dates are compared as year/month/day, so `2015-03-31` works on a
  `360_day` model and `2016-02-29` on a `noleap` one. The window must lie
  inside the run's time bounds: historical ends 2014-12-31 and `ssp*` start
  2015-01-01, so a window crossing 2014→2015 is refused (exit 2) — fetch each
  experiment's part and join them with the `concat` skill along `time`.
  Calendar windows: `resolve-time last-2w`.
- `--probe-latest` — print `none` on stdout and exit (CMIP6 has no realtime cap). No `-o`.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. Longitudes are normalized
  to the [-180, 180) convention so negative west/east values select correctly.
  Omit for the full native grid. To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

If the facets match no dataset, the error lists, for the chosen model, the
valid experiments, variables, tables, and members — each with the other facets
held fixed (plus grids when `--grid` was given). Ocean/curvilinear grids (2-D
latitude/longitude over index dims) are rejected — this fetcher handles only
regular 1-D lat/lon grids.

### Output

A consolidated weather-skills standard dataset analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)`, carrying the requested variable.

The output is fully CF-1.13 compliant. CMIP6 source data is already strongly
CF-compliant, so the transform preserves the source metadata and repairs what
mapping onto the standard dataset would otherwise break:

- **Global attrs.** The rich CMIP6 globals (`title`, `source`, `institution`,
  `references`, `tracking_id`, etc.) are preserved; `Conventions` is overwritten
  to `CF-1.13` (the source carries an older inherited value such as
  `CF-1.7 CMIP-6.0 UGRID-1.0`); a `history` line is appended; and
  `weather_skills_source=cmip6:<model>/<experiment>/<member>/<table>/<variable>/<grid>`
  plus the provenance `weather_skills_history` are added.
- **Coordinates.** `latitude`/`longitude`/`time` carry CF `standard_name`,
  `units` (`degrees_north`/`degrees_east`), and `axis` (`Y`/`X`/`T`). cf-xarray
  resolves the X/Y/T axes — verified on the way out.
- **Bounds and intervals.** The source `lat_bnds`/`lon_bnds` are dropped with
  their `bounds` attrs. Time labels are cell centres (daily at 12:00, monthly
  mid-month). A regular axis (`day`) is stamped `data_interval` (`1 day`); an
  irregular monthly axis (`Amon`) has no `data_interval` and instead carries
  `time_bounds` copied from the source cell edges (`[Jan 1, Feb 1)`), so
  `aggregate-temporal` / `convert-to-totals` weight whole calendar months.
- **Calendar.** Times are decoded as cftime in the dataset's native calendar
  (often `noleap` or `360_day`) and written as cftime, so the calendar is never
  silently coerced to `standard`. Use `convert-calendar` to align with
  observations. The written time `units` epoch is re-derived, not the source's.
- **Units.** Known air-temperature (`tas`) and precip (`pr`) kinds are converted
  to standard display units (`degree_Celsius`, `mm day-1`). Other variables
  keep source `units`/`standard_name`/`long_name`; `units` are validated with a
  real udunits check before writing.

### Memory and performance

The store is opened lazily (no dask): `--bbox` and the time window are applied
before any data is read, and only the source chunks the selection touches are
downloaded. Source chunks typically span the full globe and ~600 time steps (roughly
50–125 MB each decoded), and the whole selection is held in memory when written.
A few months over Kenya peaks at ~0.35–0.4 GB RSS, of which ~0.26 GB is the
80 MB catalog CSV. A global 1° monthly historical record (1850–2014) is ~0.4 GB
of data; global daily multi-decade pulls are many GB, so pass `--bbox` and keep
daily windows short. There is no size threshold or refusal.

### Errors

Failures are reactive and each emits a one-line actionable message:

- A catalog-CSV download/network failure reports the catalog URL and what to
  check, then exits non-zero.
- Facets that match no dataset list the available experiments, variables, and
  members for the chosen model and exit 2.
- Several matching `grid_label`s list the grids and ask for `--grid` (exit 2).
- A window extending past the run's time bounds reports the covered range
  (exit 2).
- An ocean/curvilinear (2-D lat/lon) grid is rejected with a reprojection
  message (exit 2).
- A store open or write failure surfaces the underlying error.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# GFDL-CM4 historical near-surface air temperature over a country
# (dummy bbox; use resolve-region for a real one), a multi-decade monthly slice
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment historical -v tas \
  --table Amon --start-time 1980-01-01 --end-time 2014-12-31 --bbox 5/34/-5/42 -o /tmp/cmip6_hist.zarr

# Full historical monthly record (1850–2014), global grid
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment historical -v tas \
  --table Amon --start-time 1850-01-01 --end-time 2014-12-31 -o /tmp/cmip6_full_hist.zarr

# A future scenario: monthly precipitation under ssp245, mid-century decade
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment ssp245 -v pr \
  --table Amon --start-time 2040-01-01 --end-time 2049-12-31 -o /tmp/cmip6_ssp245.zarr
```

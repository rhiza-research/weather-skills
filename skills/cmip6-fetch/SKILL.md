---
name: cmip6-fetch
description: "Fetch one CMIP6 climate-model run (a model, experiment, and variable such as tas or pr) for a date range and region from the public Pangeo CMIP6 catalog on Google Cloud, as a weather-skills Zarr. No credentials needed. Use when a task needs climate-projection grids, either historical runs (1850-2014) or future scenarios (ssp*, to 2100)."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public Pangeo CMIP6 collection from Google Cloud (gs://cmip6) over anonymous access; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - tas
    - pr
---

# cmip6-fetch

Finds one dataset in the [Pangeo CMIP6](https://pangeo-data.github.io/pangeo-cmip6-cloud/) catalog by its facets (model, experiment, variable, member, table, grid) and writes it, subset by bbox and dates, as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model ID --experiment ID -v VAR [--member ID] [--table ID] [--grid LABEL] \
  --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o OUT.zarr
```

### Arguments
- `--model` — CMIP6 `source_id`, e.g. `GFDL-CM4`.
- `--experiment` — CMIP6 `experiment_id`, e.g. `historical`, `ssp245`.
- `--variable`, `-v` — CMIP6 `variable_id`, e.g. `tas`, `pr`; also the output variable name.
- `--member` — CMIP6 `member_id`. Default: `r1i1p1f1`.
- `--table` — CMIP6 `table_id`, which sets frequency and realm (`Amon` monthly, `day` daily). Default: `Amon`.
- `--grid` — CMIP6 `grid_label` (`gn`, `gr1`). Needed only when several grids match; otherwise exits 2 listing them.
- `--start-time`, `--end-time` — inclusive date range, compared as year/month/day, so `2015-03-31` works on a `360_day` model (which has no 31st).
- `--bbox` — `N/W/S/E` decimal degrees, longitudes −180..180. Omit for the full grid.
- `--probe-latest` — print `none` and exit (CMIP6 has no latest date).
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)` with the one requested variable. Longitude is `[-180, 180)`.
- Air temperature (`tas`, `tasmax`, `tasmin`) in `degree_Celsius`; precip (`pr`) in `mm day-1`. Other variables keep source units.
- Time labels are cell centres (daily 12:00, monthly mid-month). Daily data has `data_interval="1 day"`; monthly data has `time_bounds` from the source's month edges.
- Time stays in the model's own calendar (often `noleap` or `360_day`) as cftime values.
- Source CMIP6 global attrs are kept; `weather_skills_source="cmip6:<model>/<experiment>/<member>/<table>/<variable>/<grid>"`.

### Notes
- The window must lie inside the run: `historical` ends 2014-12-31 and `ssp*` starts 2015-01-01. A window not fully inside the run exits 2; fetch each experiment separately and concatenate along `time`.
- Facets matching no dataset exit 2, listing the valid experiments, variables, tables, and members for that model.
- Only regular 1-D lat/lon grids are supported; ocean or curvilinear grids exit 2.
- Chunks span the full globe and hundreds of time steps, and the selection is held in memory. Global daily multi-decade pulls are many GB, so pass `--bbox` for daily tables.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model GFDL-CM4 --experiment ssp245 -v pr --table Amon \
  --start-time 2040-01-01 --end-time 2049-12-31 --bbox 5.5/33.5/-5/42.5 -o cmip6_pr_ssp245_kenya.zarr
```
Writes 120 monthly mean precip rates (mm day-1) over Kenya for 2040-2049 under SSP2-4.5.

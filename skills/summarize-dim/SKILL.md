---
name: summarize-dim
description: "Collapse one or more named dimensions of a weather-skills Zarr with a statistic (mean, std, min, max, sum, median), either whole or separately for each value of a label coord such as a county or season. Use for an ensemble mean or spread across members, disagreement across models, a time-mean baseline for anomalies, a regional (area) mean, or per-county, per-month, or per-season statistics, such as seasonal rainfall totals summed from complete daily totals."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# summarize-dim

Applies one statistic along the named dimensions of each data variable, removing those dimensions. With `--by`, it applies the statistic separately to each group of a label coord.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i IN.zarr -o OUT.zarr (--dim DIM ... | --by LABEL ... | both) --method mean|std|min|max|sum|median [-v VAR ...] [--lat-weighted] [--min-coverage 1.0]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr, replaced if it exists.
- `--dim` — dimension to summarize (repeatable); must be a dim of the input.
- `--by` — label coord to group by, such as one from `label-group`; repeatable (`--by admin_1 --by month_group`). Its dims collapse to one value per label; `--dim` dims collapse within each group too.
- `--method` — `mean`, `std`, `min`, `max`, `sum`, or `median`. Required.
- `--variable`, `-v` — summarize only this variable (repeatable). Default: all.
- `--lat-weighted` — with `--method mean` and the latitude dim in `--dim`: weight each row by cos(latitude), so a regional mean reflects true area. Not available with `--by`.
- `--min-coverage` — with a `--by` time label from `label-group` (`year`, `month`, `dayofyear`, `month_group`): leave out each station or cell whose data covers less than this share (0–1) of a group's calendar period. Default `1.0`.

Pass `--dim`, `--by`, or both.

### Output
- Each selected variable is summarized over the requested dims it has; other variables pass through unchanged. A summarized dim is removed once no variable carries it.
- `--by L`: the dims of `L` become a dim `L` holding each label once; several `--by` give one dim each. The `""` label is left out. Other label coords on those dims are dropped; only variables carrying all of the label's dims are summarized.
- NaNs are skipped. `sum` of an all-NaN slice is NaN, not 0. `std` is the sample standard deviation (`ddof=1`), so `std` over a dim of size 1 is NaN.
- `units` and other attrs are kept as they are, including after `sum` (no unit change). So `sum` over `time` or `step` of a rate (e.g. `mm day-1`) is refused: convert rates to period totals first (`convert-to-totals`).
- `sum` over grid cells or stations (`latitude`, `longitude`, `station_id`, by `--dim` or a spatial `--by` label such as `admin_1`) exits 2 unless the variable is dimensionless (units `1`, such as 0/1 flags, which sum to a count). For a regional value use `mean`; a regional total is a sum over time, then a `mean` by region in a second call.
- `sum` with `--min-coverage` below 1: kept sums of incomplete groups are partial-data estimates; stderr warns, and `<var>_coverage` (0–1, the least-covered cell used) is written, linked by `ancillary_variables`.

### Notes
- Write to a new path: an `--output` equal to the input destroys the input.
- `--by` gives each grid cell equal weight in a group (not area-weighted).
- A regional rainfall total takes two calls: `sum` by the time labels, then `mean` by the spatial label (see [Grouped examples](#grouped-examples)). One call with both labels and `sum` is refused.
- Keep `--min-coverage 1.0` unless a task explicitly accepts estimates. Spatial labels have no coverage check. Other labels along time are refused.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i ecmwf_s2s_kenya.zarr -o ecmwf_s2s_kenya_spread.zarr --dim number --method std
```
Writes the ensemble spread (standard deviation across the 101 members) of the ECMWF forecast, in the input units.

Grouped examples (`--by`) are under [Details](#grouped-examples).

## Details
- Coverage (with a `--by` time label) is per input station or grid cell: its time steps with data over the steps the group's full calendar period holds at the data's step (MAM of daily data: 92). Input `aggregation_coverage` counts as that fraction. A season cut by the record's start or end is incomplete. Cells below `--min-coverage` are left out before any spatial reduction (a regional value uses complete cells only, NaN if none), counted on stderr; missing steps are never filled. A repeated time step is refused, as is a step longer than a day that runs into the next group (a week from 27 May under `MAM`): aggregate to daily or monthly steps first.
- The statistic is appended to CF `cell_methods` (e.g. `number: standard_deviation`, `latitude: longitude: mean`, `latitude: longitude: mean (grouped by admin_1)`). `std` and `sum` also set `long_name` (`Standard deviation of Total precipitation across number`) and drop `GRIB_name`, `short_name`, and `comment`; `standard_name` stays.

### Grouped examples
Grouped with `--by`, on labels added by `label-group` (`--level admin_1 --time year --time month --time MAM,OND`):

```bash
# County-mean daily rainfall: (time, latitude, longitude) -> (time, admin_1)
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_labeled.zarr -o chirps_county_daily.zarr --by admin_1 --method mean

# Mean rainfall rate for each calendar month: (time, ...) -> (month, latitude, longitude)
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_labeled.zarr -o chirps_monthly_clim.zarr --by month --method mean
```

Seasonal totals need totals, not rates: aggregate to months and convert first, then label and sum.

```bash
# monthly mm totals (aggregate-temporal --period monthly, then convert-to-totals), labelled with
# label-group --level admin_1 --time year --time MAM,OND
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_monthly_mm_labeled.zarr -o chirps_season_totals.zarr --by year --by month_group --method sum
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_season_totals.zarr -o chirps_county_season_totals.zarr --by admin_1 --method mean
```
The first writes each cell's MAM and OND total in mm for every year, `(year, month_group, latitude, longitude)`; a season the record does not fully cover is NaN. The second averages those cell totals over each county, `(year, month_group, admin_1)`.

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

Reduces each data variable with one statistic, over whole dimensions or once per label (a county, a season).

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i IN.zarr -o OUT.zarr (--dim DIM ... | --by LABEL ... | both) --method METHOD [--q Q ...] [-v VAR ...] [--lat-weighted] [--min-coverage 1.0]
```

### Modes
Pass `--dim`, `--by`, or both:
- `--dim time` — collapse whole dims: a time mean in every cell.
- `--by admin_1` — one value per label: the dims `admin_1` lies on (latitude, longitude) become one `admin_1` dim.
- `--dim time --by admin_1` — both: one value per county over all times.
- Several `--by` on the same dims give each label pair (county × zone); on different dims, each combination (county × season).
- Add `--lat-weighted` (mean only) to weight by area; add `--min-coverage` to a `--by` time label to set how complete a group must be.

### Methods
Every method skips NaN.
- `mean` — the average; with `--lat-weighted`, each latitude row counts by cos(latitude).
- `std` — sample standard deviation (`ddof=1`); NaN from fewer than 2 values. `degree_Celsius` becomes `delta_degC`.
- `min`, `max`, `median` — as named.
- `quantile` — the `--q` quantiles, in input units, on a new leading `quantile` dim (`median` is `--q 0.5`); no `--lat-weighted`.
- `sum` — the total; NaN, not 0, when every value is NaN. Refused for rates over `time` or `step` (`mm day-1`: run `convert-to-totals` first), for `degree_Celsius`, and over grid cells or stations (`latitude`, `longitude`, `station_id`, by `--dim` or a spatial `--by` label such as `admin_1`) unless the variable is dimensionless (units `1`, such as 0/1 flags, which sum to a count).

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr, replaced if it exists. Use a new path, not the input.
- `--dim` — dimension to collapse (repeatable).
- `--by` — label coord to group by (repeatable), such as `admin_1` or `month_group` from `label-group`. Not a dim itself.
- `--method` — `mean`, `std`, `min`, `max`, `sum`, `median`, or `quantile`. Required.
- `--q` — quantiles for `--method quantile` (required there), each in [0, 1]: `--q 0.1 0.5 0.9`.
- `--variable`, `-v` — variable to reduce (repeatable). Default: all.
- `--lat-weighted` — area-weight a `mean` over latitude. Latitude must be in `--dim` or under a `--by` label. Default: off (every cell counts the same).
- `--min-coverage` — only with a `--by` time label (`year`, `month`, `dayofyear`, `month_group`): the share (0–1) of a group's calendar period each station or cell needs to count. Default: `1.0` (complete groups only).

### Output
- Collapsed dims are removed; each `--by` label becomes a dim holding each label once. The `""` label is left out; a label pair with no cells is NaN.
- Variables not selected, or lacking a grouped dim, pass through unchanged. Other label coords on the grouped dims are dropped.
- Units are kept, except `std` of an offset unit (above).
- `sum` with `--min-coverage` below 1 also writes `<var>_coverage` (0–1, the least-covered cell used); stderr warns that those sums are partial-data estimates.

### Notes
- A regional rainfall total takes two calls: `sum` by the time labels, then `mean` by the spatial label (see [Grouped examples](#grouped-examples)). One call with both labels and `sum` is refused.
- Keep `--min-coverage 1.0` unless a task explicitly accepts estimates. Spatial labels have no coverage check; other labels along time are refused.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i ecmwf_s2s_kenya.zarr -o ecmwf_s2s_kenya_spread.zarr --dim number --method std
```
Writes the ensemble spread (standard deviation across the 101 members) of the ECMWF forecast, in the input units.

Grouped examples (`--by`) are under [Details](#grouped-examples).

## Details
- Coverage is per input station or grid cell: its steps with data over the steps the group's full calendar period holds at the data's step (MAM of daily data: 92). An input `aggregation_coverage` counts as that fraction of a step. A season cut by the record's start or end is incomplete. Cells below `--min-coverage` are left out before cells are pooled, so a regional value uses complete cells only (NaN if none); stderr counts them. Missing steps are never filled. A repeated time step is refused, as is a step longer than a day that runs into the next group (a week from 27 May under `MAM`): aggregate to daily or monthly steps first.
- The statistic is appended to CF `cell_methods` (`number: standard_deviation`, `latitude: longitude: mean (grouped by admin_1)`). `std` and `sum` also set `long_name` (`Standard deviation of Total precipitation across number`) and drop `GRIB_name`, `short_name`, and `comment`; `standard_name` stays.

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

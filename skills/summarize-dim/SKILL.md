---
name: summarize-dim
description: "Collapse one or more named dimensions of a weather-skills Zarr with a statistic (mean, std, min, max, sum, median, quantile), either whole or separately for each value of a label coord such as a county or season. Use for an ensemble mean, spread, or percentile band across members, disagreement across models, a time-mean baseline for anomalies, a regional (area) mean, or per-county, per-month, or per-season statistics, such as seasonal rainfall totals summed from complete daily totals."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py *)
metadata:
  version: "0.0.15"
  catalog-group: transforms
---

# summarize-dim

Applies one statistic along named dims, removing them. With `--by`, applies it separately to each group of a label coord.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i IN.zarr -o OUT.zarr (--dim DIM ... | --by LABEL ... | both) --method mean|std|min|max|sum|median|quantile [-q Q ...] [-v VAR ...] [--lat-weighted] [--min-coverage 1.0]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr, replaced if it exists. Use a new path; the input's own path destroys it.
- `--dim` — dim to collapse (repeatable).
- `--by` — label coord to group by, such as one from `label-group` (repeatable: `--by admin_1 --by month_group`). Its dims collapse to one value per label; `--dim` dims collapse within each group. Grid cells weigh equally (not by area).
- `--method` — `mean`, `std`, `min`, `max`, `sum`, `median`, or `quantile`. Required.
- `--quantile`, `-q` — with `quantile`: a value in 0–1 (repeatable, `-q 0.1 -q 0.9`; one only with `--by`).
- `--variable`, `-v` — summarize only this variable (repeatable). Default: all.
- `--lat-weighted` — with `mean` over latitude: weight rows by cos(latitude) for a true area mean. Not with `--by`.
- `--min-coverage` — for a `--by` time label, or a `sum` over `time`/`step` without `--by`: groups or cells with less than this fraction (0–1) of their expected steps become NaN. Default `1.0`: no gaps. Keep it unless the task accepts estimates. Steps that are not on one regular grid (weeks from 1 Feb of each year) need `0`: each group then uses the steps it has.

### Output
- Selected variables lose the summarized dims; other variables pass through with all their dims, so a dim stays while any of them carries it. Drop unneeded variables (drop) first to remove it.
- `--by L`: one entry per label along a new dim `L`; the `""` label is left out, other label coords on those dims are dropped, and only variables with all of `L`'s dims are summarized.
- `quantile`: one `-q` gives a scalar `quantile` coord; several give a `quantile` dim.
- NaNs are skipped. A `sum` of all NaN is NaN; `std` is the sample std (`ddof=1`).
- Units are unchanged, so `sum` over `time` or `step` of a rate (`mm day-1`) is refused: convert to totals first (convert-to-totals). `sum` over grid cells or stations is refused unless units are `1` (0/1 flags give a count); for a regional total, sum over time, then `mean` by region in a second call.
- `sum` with `--min-coverage` below 1 also writes `<var>_coverage` (0–1) and warns that partial sums are estimates.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i ecmwf_s2s_kenya.zarr -o ecmwf_s2s_kenya_spread.zarr --dim number --method std
```
Writes the ECMWF ensemble spread (std across the 101 members), in the input units.

## Details
- **Time groups.** Each step goes in the group where its cell starts: the start of its `time_bounds` cell if it has one (a week labelled by its last day counts from its first day), else its time label. Overlapping cells (rolling windows) go by their time label, as in label-group. `--by` time labels must be label-group's `year`, `month`, `dayofyear` or `month_group`. A repeated step is refused, as is a step longer than a day that crosses into the next group (a week from 27 May under `MAM`); aggregate to daily or monthly steps first. Month-long cells count as monthly steps.
- **Coverage** is the fraction of expected steps that have data, at each cell or station. A `--by` group expects every step of its calendar period (92 for MAM of daily data), so a season cut off by the start or end of the record is incomplete; a `sum` without `--by` expects every step it adds. A step with `aggregation_coverage` counts as that fraction. Values below `--min-coverage` become NaN before any further reduction; gaps are never filled.
- **Attributes.** The statistic is added to `cell_methods` (`number: standard_deviation`, `latitude: longitude: mean (grouped by admin_1)`). `std` and `sum` set `long_name` and drop `GRIB_name`, `short_name` and `comment`. A `sum` over `time` or `step` stamps the summed span as `aggregation_period`: ten `1 day` steps give `10 day`, daily by MAM `92 day`, the longest group's when lengths differ, every year's run when no `year` label (three MAMs: `276 day`); calendar steps keep their unit (`3 month`).

### Grouped examples
On labels from `label-group --level admin_1 --time year --time month --time MAM,OND`:

```bash
# County-mean daily rainfall: (time, lat, lon) -> (time, admin_1)
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_labelled.zarr -o chirps_county_daily.zarr --by admin_1 --method mean

# Mean rate for each calendar month: (time, lat, lon) -> (month, lat, lon)
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_labelled.zarr -o chirps_monthly_clim.zarr --by month --method mean
```

Seasonal totals need totals: aggregate to months (aggregate-temporal --period monthly), convert-to-totals, label, then sum.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_monthly_mm_labelled.zarr -o chirps_season_totals.zarr --by year --by month_group --method sum
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_season_totals.zarr -o chirps_county_season_totals.zarr --by admin_1 --method mean
```
The first gives each cell's MAM and OND total in mm per year (NaN for a season the record does not fully cover); the second averages them over each county.

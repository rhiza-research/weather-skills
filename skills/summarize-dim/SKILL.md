---
name: summarize-dim
description: "Collapse one or more named dimensions of a weather-skills Zarr with a statistic (mean, std, min, max, sum, median, quantile), either whole or separately for each value of a label coord such as a county or season. Use for an ensemble mean, spread, or percentile band across members, disagreement across models, a time-mean baseline for anomalies, a regional (area) mean, or per-county, per-month, or per-season statistics, such as seasonal rainfall totals summed from complete daily totals."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py *)
metadata:
  version: "0.0.13"
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
- `--min-coverage` — for a `--by` time label or a `sum` over `time`/`step`: groups or cells with less than this fraction (0–1) of their expected steps become NaN. Default `1.0`: no gaps. Keep it unless the task accepts estimates.

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
- **Time groups.** A step belongs to the group its cell starts in: the start of its CF `time_bounds` cell when it has one (a week labelled by its last day goes by its first day), else its label, as `label-group` writes it. Labels along time must be `label-group`'s `year`, `month`, `dayofyear` or `month_group`. A repeated step, or a step longer than a day that runs into the next group (a week from 27 May under `MAM`), is refused: aggregate to daily or monthly steps first. Whole calendar months (month-long bounds, or month-start labels) count as monthly steps.
- **Coverage** is the share of expected steps with data at each point: a `--by` group's full calendar period at the data's step (MAM of daily data: 92; a season cut by the record's ends is incomplete), or every summed step. Input `aggregation_coverage` counts as that fraction of a step. Data below `--min-coverage` is left out before any further reduction (a regional mean uses complete cells only), counted on stderr; missing steps are never filled.
- **Attributes.** The statistic is added to `cell_methods` (`number: standard_deviation`, `latitude: longitude: mean (grouped by admin_1)`). `std` and `sum` set `long_name` and drop `GRIB_name`, `short_name` and `comment`. A `sum` over `time` or `step` stamps the summed span as `aggregation_period`: ten `1 day` steps give `10 day`, daily by MAM `92 day`, the longest group's when lengths differ, every year's run when no `year` label (three MAMs: `276 day`); calendar steps keep their unit (`3 month`).

### Grouped examples
On labels from `label-group --level admin_1 --time year --time month --time MAM,OND`:

```bash
# County-mean daily rainfall: (time, lat, lon) -> (time, admin_1)
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_labeled.zarr -o chirps_county_daily.zarr --by admin_1 --method mean

# Mean rate for each calendar month: (time, lat, lon) -> (month, lat, lon)
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_labeled.zarr -o chirps_monthly_clim.zarr --by month --method mean
```

Seasonal totals need totals: aggregate to months (aggregate-temporal --period monthly), convert-to-totals, label, then sum.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_monthly_mm_labeled.zarr -o chirps_season_totals.zarr --by year --by month_group --method sum
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i chirps_season_totals.zarr -o chirps_county_season_totals.zarr --by admin_1 --method mean
```
The first gives each cell's MAM and OND total in mm per year (NaN for a season the record does not fully cover); the second averages them over each county.

---
name: regularize-time
description: "Put a weather-skills Zarr whose time or lead (step) samples overlap or are irregular (one weekly mean per forecast init, inits days apart; windows of mixed lengths) onto a regular grid at a chosen period (1 day, 6 hour, 15 minute), averaging the samples that cover each grid cell and stamping how complete each cell is. Use before aggregate-temporal when samples overlap, or to put irregular windows on an even axis."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/regularize_time.py *)
metadata:
  version: "0.0.3"
  catalog-group: transforms
---

# regularize-time

Turns samples that each cover an interval, possibly overlapping, into one value per grid cell of `--period`.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/regularize_time.py -i IN.zarr -o OUT.zarr --period PERIOD [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — Zarr with a `time` or `step` axis. Each sample covers its CF `{dim}_bounds`, or `[t, t + interval)` where the interval is each variable's `aggregation_period`, else its `data_interval`. Variables on the axis with different intervals exit 2.
- `--output`, `-o` — output Zarr.
- `--period` — grid spacing, a fixed duration: `1 day`, `6 hour`, `15 minute`. Every sample start and end must fall on a grid edge; pick the longest period that does (whole-day windows take `1 day`, also when they run 06–06 UTC). Too long a period exits 2 and names the longest one that fits. To coarsen (15-minute data to daily), run it at the sample period, then aggregate-temporal.
- `--time-dim` — axis to regularize. Default: `step` when the dataset has one and `time` has at most one value, else `time`. A `time` axis of forecast inits (`standard_name: forecast_reference_time`) exits 2: run step-to-time first.

### Output
- A regular axis from the earliest sample start to the latest sample end, labelled at each cell's left edge; 06–06 UTC days stay 06–06.
- Each cell is the plain mean of every sample that covers it: days 1–7, 2–8 and 3–9 give day 2 the mean of the first two and day 5 the mean of all three. Cells no sample covers are NaN.
- Coordinate `aggregation_coverage` (0–1) per cell: the mean coverage of the samples covering it (a sample's own `aggregation_coverage`, per sample or one value for all, else 1; 0 if any variable is all-NaN at that sample). Uncovered cells are 0.
- Each variable gets `aggregation_period` and `data_interval` equal to the period, and `cell_methods` ending in `{dim}: mean`, with `comment: overlapping samples` when a cell averages more than one sample or the input already has it. Methods on other dims (`number: mean`) stay. `{dim}_bounds` and other coords along the axis are dropped.

### Notes
- A grid point missing a sample that other points in the same cell have is NaN. A variable with no other dims averages the samples it has; `aggregation_coverage` shows what is missing.
- Takes rates (`mm day-1`) and means. Precipitation amounts, and `cell_methods` with `maximum`, `minimum` or `sum`, exit 2. Integer variables come out as floats.
- Calendar periods (months, years) and cftime model calendars exit 2.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/regularize_time.py -i ecmwf_week1_by_init.zarr -o ecmwf_week1_daily.zarr --period "1 day"
```
Week-1 mean rates from daily inits (each `[init, init + 7 days)`, so consecutive weeks overlap by 6 days) become one daily rate per valid date, averaged over the weeks covering that day. `aggregate-temporal --period monthly` on the output then gives calendar months with their coverage.

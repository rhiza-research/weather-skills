---
name: resample-time
description: "Put a weather-skills Zarr whose time or lead (step) samples overlap or are irregular (one weekly mean per forecast init, inits days apart; windows of mixed lengths) onto a regular grid at a chosen period (1 day, 6 hour, 15 minute), averaging the samples that cover each grid cell and stamping how complete each cell is. Use before aggregate-temporal when samples overlap, or to put irregular windows on an even axis."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resample_time.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# resample-time

Turns samples that each cover an interval, possibly overlapping, into one value per grid cell of `--period`.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resample_time.py -i IN.zarr -o OUT.zarr --period PERIOD [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — Zarr with a `time` or `step` axis. Each sample covers its CF `{dim}_bounds`, or `[t, t + interval)` where the interval is the variable's `aggregation_period`, else the dataset's `data_interval`.
- `--output`, `-o` — output Zarr.
- `--period` — grid spacing, a fixed duration: `1 day`, `6 hour`, `15 minute`. Every sample start and end must fall on a grid edge; pick the longest period that does (whole-day windows take `1 day`, windows that start at 06 UTC take `6 hour` or shorter). Too long a period exits 2 and names the longest one that fits.
- `--time-dim` — axis to resample. Default: `step` when the dataset has one and `time` has at most one value, else `time`. A `time` axis of forecast inits (`standard_name: forecast_reference_time`) exits 2: run step-to-time first.

### Output
- A regular axis from the earliest sample start to the latest sample end, one label per cell at its left edge. Grid edges count from 00 UTC on `time` and from lead 0 on `step`.
- Each cell is the plain mean of every sample that covers it: days 1–7, 2–8 and 3–9 give day 2 the mean of the first two and day 5 the mean of all three. Cells no sample covers are NaN.
- Coordinate `aggregation_coverage` (0–1) per cell: the mean coverage of the samples covering it (a sample's own `aggregation_coverage`, or 1; 0 if it has no data anywhere). Uncovered cells are 0.
- Each variable gets `aggregation_period` and `data_interval` equal to the period, and `cell_methods` `{dim}: mean`, with `comment: overlapping samples` when samples overlapped (or the input already carried it). `{dim}_bounds` and other coords along the axis are dropped.

### Notes
- A cell missing a sample that other cells have is NaN for that grid cell, not a mean of the samples it has.
- Values are averaged, so pass rates (`mm day-1`) or means, not totals or extremes. Integer variables come out as floats.
- Calendar months and years are not fixed lengths and are refused, as are cftime model calendars.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resample_time.py -i ecmwf_week1_by_init.zarr -o ecmwf_week1_daily.zarr --period "1 day"
```
Week-1 mean rates from daily inits (each `[init, init + 7 days)`, so consecutive weeks overlap by 6 days) become one daily rate per valid date, averaged over the weeks covering that day. `aggregate-temporal --period monthly` on the output then gives calendar months with their coverage.

---
name: inspect-zarr
description: "Print a short summary of a weather-skills Zarr: dimension sizes, coordinate values, and for each data variable its dims, dtype, units, key attributes, min/max/mean, finite and NaN counts, and a small sample of values. Use to see what a Zarr holds, or to check values and units after a fetch or transform."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py *)
metadata:
  version: "0.0.4"
  catalog-group: agent-tooling
---

# inspect-zarr

Prints the structure of a Zarr and a bounded summary of its data; it writes nothing.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py -i IN.zarr [--format human|json] [--max-values N] [--skip-nan]
```

### Arguments
- `--input`, `-i` — any weather-skills Zarr (gridded, forecast, or station).
- `--format` — `human` or `json`. Default: `human`.
- `--max-values` — values shown per coordinate and per data-variable sample. Default: `24`. Longer coordinates show the first and last values plus the total count. `0` shows every coordinate value; data samples stay capped at 256.
- `--skip-nan` — sample the first finite values instead of the first cells. Off by default.

### Output
- `Dimensions`: each dim and its size.
- `Coordinates`: name, dims, dtype, units, and values. Times print as dates; forecast `step` prints as durations (`7 days`, `0 days 03:00:00`).
- `Data variables`: name, dims, dtype, shape, units; the attrs `long_name`, `standard_name`, `cell_methods`, `aggregation_period`, `data_interval` when set; `finite N/M`, `min`, `max`, `mean`; and a sample: the first `--max-values` cells (256 with `0`) in order, NaN included, so it lines up with the coordinate values; with `--skip-nan`, the first finite values. A cut-off sample ends `(K of N cells)` (`(K of N finite cells)` with `--skip-nan`). Datetime variables get NaT counts and the date range, with no mean.
- `json`: the same as `{"dims", "coords", "data_vars"}`. Coord entries have `values`, `truncated`, `size`; variable entries have `attrs`, `n`, `n_finite`, `n_nan`, `min`, `max`, `mean`, `sample`, `sample_truncated`. NaN statistics are `null`.

### Notes
- The whole input is read into memory for the statistics, so a large store is slow; output stays small. Subset a large store first if you only need part of it.
- The processing history (`weather_skills_history`) is not printed.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py -i chirps_kenya.zarr
```
Prints the time, latitude and longitude axes of the Kenya CHIRPS grid and the range, NaN count, and a sample of `precip`.

---
name: summarize-dim
description: "Collapse one or more named dimensions of a weather-skills Zarr with a statistic (mean, std, min, max, sum, median); the summarized dimension disappears from the output. Use for an ensemble mean or spread across members, disagreement across models, a time-mean baseline for anomalies, or a regional (area) mean."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# summarize-dim

Applies one statistic along the named dimensions of each data variable, removing those dimensions.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i IN.zarr -o OUT.zarr --dim DIM [--dim DIM ...] --method mean|std|min|max|sum|median [-v VAR ...] [--lat-weighted]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr, replaced if it exists.
- `--dim` — dimension to summarize; repeat for several. Required. Each must be a dim of the input.
- `--method` — `mean`, `std`, `min`, `max`, `sum`, or `median`. Required.
- `--variable`, `-v` — summarize only this variable; repeat for several. Default: every data variable.
- `--lat-weighted` — with `--method mean` and the latitude dim in `--dim`: weight each row by cos(latitude), so a regional mean reflects true area.

### Output
- Each selected variable is summarized over the requested dims it has; variables with none of them, and unselected variables, pass through unchanged. A summarized dim and its coords are removed once no variable carries it.
- NaNs are skipped. `sum` of an all-NaN slice is NaN, not 0. `std` is the sample standard deviation (`ddof=1`), so `std` over a dim of size 1 is NaN.
- `units` and other attrs are kept as they are, including after `sum` (no unit change).
- The statistic is appended to CF `cell_methods` (e.g. `number: standard_deviation`, `latitude: longitude: mean`). `std` and `sum` also set `long_name` (e.g. `Standard deviation of Total precipitation across number`) and drop `GRIB_name`, `short_name`, and `comment`; `standard_name` stays.

### Notes
- Write to a new path: an `--output` equal to the input destroys the input.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i ecmwf_s2s_kenya.zarr -o ecmwf_s2s_kenya_spread.zarr --dim number --method std
```
Writes the ensemble spread (standard deviation across the 101 members) of the ECMWF forecast over Kenya, in the input units.

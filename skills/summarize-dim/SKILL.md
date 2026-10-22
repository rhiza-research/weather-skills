---
name: summarize-dim
description: "Collapse one or more named dimensions of a weather-skills Zarr with a statistic (mean, std, min, max, sum, median, quantile), either whole or separately for each value of a label coord such as a county or season. Use for an ensemble mean, spread, or percentile band across members, disagreement across models, a time-mean baseline for anomalies, a regional (area) mean, or per-county, per-month, or per-season means and totals."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py *)
metadata:
  version: "0.0.3"
  catalog-group: transforms
---

# summarize-dim

Applies one statistic along the named dimensions of each data variable, removing those dimensions. With `--by`, it applies the statistic separately to each group of a label coord.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i IN.zarr -o OUT.zarr (--dim DIM ... | --by LABEL ... | both) --method mean|std|min|max|sum|median|quantile [-q Q ...] [-v VAR ...] [--lat-weighted]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr, replaced if it exists.
- `--dim` — dimension to summarize; repeat for several. Each must be a dim of the input.
- `--by` — label coord to group by, such as one written by `label-group`; repeat for several (`--by admin_1 --by month_group`). The dims the label spans are collapsed to one value per label; `--dim` dims are collapsed within each group too.
- `--method` — `mean`, `std`, `min`, `max`, `sum`, `median`, or `quantile`. Required.
- `--quantile`, `-q` — with `--method quantile`: a quantile between 0 and 1; repeat for several (`-q 0.1 -q 0.9` for a 10–90% band). One with `--by`.
- `--variable`, `-v` — summarize only this variable; repeat for several. Default: every data variable.
- `--lat-weighted` — with `--method mean` and the latitude dim in `--dim`: weight each row by cos(latitude), so a regional mean reflects true area. Not available with `--by`.

Pass `--dim`, `--by`, or both.

### Output
- Each selected variable is summarized over the requested dims it has; variables with none of them, and unselected variables, pass through unchanged. A summarized dim and its coords are removed once no variable carries it.
- `--by L`: the dims of `L` are replaced by a dim `L` holding each label once; several `--by` give one dim each. The `""` label (outside every unit or group) is left out. Other label coords on those dims are dropped, and only variables that carry all of the label's dims are summarized.
- `quantile`: one `-q` leaves a scalar `quantile` coord; several add a `quantile` dim, one entry per value, in the order given.
- NaNs are skipped. `sum` of an all-NaN slice is NaN, not 0. `std` is the sample standard deviation (`ddof=1`), so `std` over a dim of size 1 is NaN.
- `units` and other attrs are kept as they are, including after `sum` (no unit change).
- The statistic is appended to CF `cell_methods` (e.g. `number: standard_deviation`, `number: quantile 0.1, 0.9`, `latitude: longitude: mean`, `latitude: longitude: mean (grouped by admin_1)`). `std` and `sum` also set `long_name` (e.g. `Standard deviation of Total precipitation across number`) and drop `GRIB_name`, `short_name`, and `comment`; `standard_name` stays.

### Notes
- Write to a new path: an `--output` equal to the input destroys the input.
- `--by` gives each grid cell equal weight within a group; it is not area-weighted.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i ecmwf_s2s_kenya.zarr -o ecmwf_s2s_kenya_spread.zarr --dim number --method std
```
Writes the ensemble spread (standard deviation across the 101 members) of the ECMWF forecast over Kenya, in the input units.

---
name: concat
description: Concatenate two or more weather-skills standard dataset Zarr stores along a named dimension, optionally assigning coordinate values to the new axis. Use when combining ensemble members, stitching consecutive time windows, or stacking models along a new dim. All other coordinates must match exactly (same grid, same times); overlapping or mismatched inputs exit 2 rather than NaN-padding.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# concat

Source-agnostic concatenation along a named dim. If the concat dim does not exist on the inputs, a new dim is created (use `--coords` to assign values).

Every other coordinate must match exactly — same lat/lon grid, same times,
same stations. A mismatch exits 2 instead of padding with NaN; put inputs on
one grid first (`coarsen --reference-grid`) and trim windows (`select`,
`clip-region`).

## When to use

- Stitching consecutive time windows (e.g. two fetches) along `time`.
- Joining two ensemble halves (cf and pf) along `number`.
- Stacking the same window from several models along a new `model` dim.
- Stitching adjacent boxes along `longitude` or `latitude` that share the
  other axis exactly and leave no gap.

Not for merging overlapping regions: two country bboxes overlap and sit on
different latitude rows, so they are refused. Fetch one bbox covering both.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i a.zarr -i b.zarr [-i ...] --dim DIM --output <out.zarr> \
    [--coords V1,V2,...]
```

### Arguments
- `--input`, `-i` — input Zarr (pass once per input; order is preserved). At least two are required.
- `--dim` — dimension name to concatenate along.
- `--coords` — optional comma-separated coord values to assign to the new dim. Length must match number of inputs. Exits 2 if `--dim` already exists on the inputs. Values parse as int, float, ISO date (`2026-09-01`), else text. Without it the new dim has no coordinate labels — e.g. two single-day `select` outputs concatenated along `time` lose their dates, so pass them here.
- `--output`, `-o` — output Zarr.

### Input units

The output carries each variable in the first input's units. A later input in
compatible units is converted (e.g. `mm hr-1` → `mm day-1`); incompatible
units (e.g. `K` against `mm day-1`) exit non-zero with no output.

### Refusals (exit 2, no output)

- Labels repeated along an existing dim — overlapping windows such as
  Sep 1–7 and Sep 5–10. Drop the overlap from one input with `select`.
- `--dim` present on some inputs but not others.
- Inputs carrying different variables (one has `tmax`, another doesn't);
  xarray would fill the missing one with NaN.
- A stitched `latitude` / `longitude` that is not evenly spaced: boxes with a
  gap between them, or on different grids.
- A variable's `data_interval` or `aggregation_period` differing across
  inputs (e.g. daily and weekly).

### Output

A single Zarr with the concat dim extended, sorted ascending when the inputs
were passed out of order. Attrs from the first input are preserved.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i /tmp/cf.zarr -i /tmp/pf.zarr --dim number --output /tmp/ens.zarr
```

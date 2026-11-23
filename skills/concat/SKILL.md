---
name: concat
description: "Join two or more weather-skills Zarrs into one along a named dimension, either extending an existing dim (consecutive time windows, ensemble halves, adjacent boxes) or stacking them along a new dim with optional labels (e.g. one entry per model). Use when combining ensemble members, stitching time windows, or putting several models side by side in one dataset."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py *)
metadata:
  version: "0.0.14"
  catalog-group: transforms
---

# concat

Concatenates datasets along one dim, creating the dim when the inputs lack it.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i A.zarr -i B.zarr [-i ...] --dim DIM -o OUT.zarr [--coords V1,V2,...]
```

### Arguments
- `--input`, `-i` — input Zarr; repeat once per input, at least two. Order is kept.
- `--dim` — dim to concatenate along. If no input has it, a new dim is created.
- `--coords` — new dim only: comma-separated labels, one per input. Each parses as int, float, ISO date (`2026-10-01`), else text. Default: no labels.
- `--output`, `-o` — output Zarr.

### Output
One dataset with `--dim` extended. Along time, init time, lead time or day of year the result runs ascending, whatever the input order. Along lat, lon, x, y or vertical level it is sorted ascending only when the inputs come out of order, so levels running one way (1000 to 200 hPa) keep that direction; any other dim (models, members, stations) keeps the input order and the `--coords` order. Each variable is in the first input's units; later inputs in compatible units are converted (`mm hr-1` → `mm day-1`). Attrs come from the first input. When some inputs carry `aggregation_coverage`, inputs without it get coverage 1.

### Notes
- Every other coordinate must match exactly: same grid, same times, same stations. Put inputs on one grid and window first.
- These exit 2 with no output:
  - a label repeated along an existing dim (overlapping windows such as Oct 1–7 and Oct 5–10);
  - `--dim` on some inputs but not others, or `--coords` with a dim that already exists;
  - inputs with different variables, or a variable whose `data_interval` or `aggregation_period` differs between inputs (an input without `aggregation_period` counts as its `data_interval`, so daily values aggregated to `1 day` join a native daily forecast);
  - incompatible units (`K` against `mm day-1`);
  - dates on different calendars (standard against `noleap`; use convert-calendar);
  - a stitched `latitude` / `longitude` that is not evenly spaced (a gap between boxes, or different grids).
- A dim other than `--dim` that only some inputs have (an ensemble's `number`) is broadcast: inputs without it are copied to every entry. This joins observations to each member of a forecast ensemble (recipe below).
- Scalar coordinates (the `time` or `step` that select keeps) that differ between inputs become a coordinate along `--dim`; one only some inputs carry is dropped. So single-time inputs stacked along `--dim time` keep their dates; inputs with no time label have none unless you pass them with `--coords`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i ecmwf_w2_kenya.zarr -i gefs_w2_kenya.zarr --dim model --coords ecmwf,gefs -o models_w2_kenya.zarr
```
Writes week-2 precip from both models on one grid, stacked along a new `model` dim labelled `ecmwf`, `gefs`.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i obs_daily.zarr -i fc_ens_valid.zarr --dim time -o obs_plus_fc.zarr
```
Joins observed days to each member of a forecast ensemble, e.g. to run an indicator from observations into the forecast. `fc_ens_valid.zarr` is the ensemble after step-to-time, so its `time` holds valid dates starting the day after the observations end; both share one grid, one variable name and daily values. The output keeps the forecast's `number` dim and every member starts with the same observed days. Join each forecast separately: ensembles with different member counts cannot share one `number` axis.

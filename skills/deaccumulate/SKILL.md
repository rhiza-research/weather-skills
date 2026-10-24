---
name: deaccumulate
description: "Convert a forecast variable stored as a running total since the forecast start (init) into per-step values along its lead (step) axis; precipitation amounts become mm day-1 rates. Use on archives whose precipitation or radiation still grows with lead time and has amount units such as mm, m, or J m-2."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# deaccumulate

Differences a cumulative-since-init forecast variable between consecutive steps, clipped at zero.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py -i IN.zarr -o OUT.zarr [-v VAR]
```

### Arguments
- `--input`, `-i` — forecast Zarr with a `step` axis of at least two strictly increasing leads.
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — variable to deaccumulate. Required when the input has more than one data variable.

### Output
- `step` is one shorter: each value covers `[step[i], step[i+1])` and is labeled at its left edge; the last input step is dropped. `valid_time`, if present, is recomputed as `time + step`.
- Precipitation amounts (`mm`, `m`, `kg m-2`) become `mm day-1` (`lwe_precipitation_rate`), each increment divided by its own interval length. Other variables (e.g. `ssrd` in `J m-2`) become per-interval increments in their input units.
- Increments below zero become 0. NaN stays NaN.
- Evenly spaced steps get `data_interval` (e.g. `1 day`); uneven steps get CF `step_bounds` (start and end of each interval).
- Other data variables are not differenced; they are cut to the same steps.

### Notes
- Input must be a non-negative running total. Exits 2 when the variable has a rate unit (`mm day-1`, `kg m-2 s-1`, `W m-2`) or a `standard_name` ending in `_rate` / `_flux`, or has negative values (e.g. ECMWF evaporation `e`, net radiation `str`).
- A variable with no `units` and no `standard_name` cannot be checked; it is processed with a warning on stderr.
- Without a step 0, the amount before the first step is not recovered.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py -i ecmwf_tp_cumulative_kenya.zarr -o ecmwf_tp_rates_kenya.zarr -v tp
```
Writes per-step `tp` rates in `mm day-1` from a cumulative ECMWF precipitation archive over Kenya.

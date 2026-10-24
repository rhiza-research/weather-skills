---
name: coarsen
description: "Put a gridded weather-skills Zarr onto a coarser or equally spaced lat/lon grid, copied from another Zarr or built from a resolution and offset, by linear interpolation or an area-weighted (conservative) mean. Use to make two grids match cell for cell before subtracting or scoring them, e.g. fine satellite rainfall onto a coarse forecast grid, or to shift a grid by half a cell."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py *)
metadata:
  version: "0.0.5"
  catalog-group: transforms
---

# coarsen

Regrids a dataset onto a target lat/lon grid that is coarser than, or as fine as, the input.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i IN.zarr -o OUT.zarr (--reference-grid REF.zarr | --target-resolution DEG --offset DEG) [--method linear|conservative] [--min-fraction F] [-v NAME]
```

### Arguments
- `--input`, `-i` — input Zarr with lat and lon.
- `--output`, `-o` — output Zarr.
- `--reference-grid` — Zarr whose exact lat/lon values become the target, kept to the input's extent (plus half a cell).
- `--target-resolution` — target spacing in degrees. Needs `--offset`.
- `--offset` — target points fall at `offset + k * resolution` on both axes. Needs `--target-resolution`.
- `--method` — `linear` reads the input at each target point; `conservative` averages every input cell inside each target cell, weighted by overlap area and cos(latitude). Default: `linear`.
- `--min-fraction` — `conservative` only: the least share of a target cell's area that must hold finite input data, in (0, 1]. Default: 1.0, so any missing input cell, or any part of the cell past the input's edge, makes the target cell NaN. stderr reports how many target cells hang past the input's edge; to keep them, fetch the input half a target cell wider on each side (e.g. 0.75° for a 1.5° grid) or lower `--min-fraction`.
- `--variable`, `-v` — regrid only this data variable. Default: all.

Pass `--reference-grid`, or `--target-resolution` with `--offset`.

### Output
The input with lat/lon replaced by the target grid. Other dims (`time`, `step`, `number`), attrs, and variables without both lat and lon pass through unchanged. Longitudes in `[0, 360]` (input and reference) are wrapped to `[-180, 180)`. With `linear`, target points outside the input's span are NaN.

### Notes
- Use `--reference-grid` to match another dataset: it copies that grid's values exactly, so cell-by-cell joins line up. A grid rebuilt from resolution and offset can differ in the last float digits.
- Use `conservative` when the target is much coarser (0.05° onto 1.5°): `linear` keeps only the few input cells next to each target point. Use `linear` for same-resolution shifts and small ratios.
- A target finer than the input on either axis exits with an error. Spacings within 1% count as equal, so a half-cell shift at the same resolution is accepted.
- Sheerwater grid alignments: `0.25`/`0.0` (global0_25), `0.1`/`0.05` (global0_1), `0.05`/`0.025` (global0_05).

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i chirps_kenya.zarr -o chirps_on_ecmwf.zarr --reference-grid ecmwf_s2s_kenya.zarr --method conservative
```
Writes CHIRPS 0.05° rainfall averaged onto the ECMWF 1.5° Kenya grid points.

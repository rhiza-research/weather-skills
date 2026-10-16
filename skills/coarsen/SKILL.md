---
name: coarsen
description: "Coarsen or align a weather-skills standard dataset Zarr onto a target grid, by linear interpolation (default; realigns and small ratios) or --method conservative (area-weighted mean of the source cells inside each target cell). Use --method conservative to put fine obs (CHIRPS/IMERG 0.05–0.1°) on a coarse forecast grid (1.5°) before verify or difference — linear samples a few source cells, not the box mean. Missing source cells make the target cell NaN unless --min-fraction is lowered. Prefer --reference-grid PATH to copy another Zarr's exact lat/lon (avoids float mismatch); or --target-resolution with --offset (points at offset + k*resolution). Near-equal resolution is a lateral realign (incl. a half-cell shift). Adds no information. Do not coarsen just to draw two datasets side by side."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# coarsen

Source-agnostic spatial coarsening and alignment onto a target lat/lon grid,
by linear interpolation (default) or a conservative area mean
(`--method conservative`). It adds no information, and is used to coarsen a
grid or to align two grids before `difference` or `verify`.

**Pick the method by the resolution ratio.** Linear interpolation reads the
input at each target point, so going from 0.05° to 1.5° uses the few source
cells next to each target centre and throws away the other ~899 of 900:
the result is a noisy point sample, not the cell's rainfall. To compare fine
observations with a coarse forecast, use `--method conservative`, which
averages every source cell inside each target cell (weighted by overlap area
and cos-latitude). Keep linear for realigning grids of about the same
resolution (a half-cell shift) or for smooth fields at small ratios. Plotting does not need that alignment. The target
must be coarser-or-equal to the input on each axis;
equal (or near-equal) resolution is accepted as a lateral realign — same
spacing, different cell-center offset, including a half-cell shift. A
meaningfully-finer target is rejected with a pointer to the `downscale`
skill. Near-equal spacings (within 1%, including float rounding or a
slightly irregular CHIRPS axis vs a regular 0.05° forecast) are not treated
as finer, so this skill and `downscale` do not ping-pong.

**Matching another dataset.** Prefer `--reference-grid other.zarr`. That
copies the other store's lat/lon values bit-for-bit (clipped to the input
extent), so `difference` / `verify` inner-joins do not collapse from
floating-point mismatch. Rebuilding `offset + k * resolution` in float64
often lands *near* IMERG/CHIRPS/ECMWF points without matching them exactly.

**Synthetic sheerwater-style grids.** When you have no reference Zarr,
`(--target-resolution 0.25, --offset 0.0)` aligns with `global0_25`;
`(0.1, 0.05)` with `global0_1`; `(0.05, 0.025)` with `global0_05`.

## When to use

- Putting obs onto a forecast grid (or the reverse) before `difference` /
  `verify` — use `--reference-grid` on the dataset you want to match.
  This includes a same-resolution lateral shift (CHIRPS 0.05° onto the
  Kenya weekly downscale, or the reverse).
- Coarsening to a larger spacing before ensemble aggregation.
- Producing output on a named sheerwater grid via `(resolution, offset)`.

Not for: drawing two datasets side by side — a map can keep each on its own lat/lon. Not for making a grid finer / adding
information — that is the `downscale` skill. Nearest, cubic, and
most-common regridding are not offered.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py --input <in.zarr> --output <out.zarr> \
    (--reference-grid REF.zarr | --target-resolution DEG --offset DEG) \
    [--method linear|conservative] [--min-fraction F] [--variable NAME]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded dataset).
- `--output`, `-o` — output Zarr.
- `--reference-grid` — Zarr whose lat/lon define the target (exact values,
  clipped to the input extent). Preferred when matching another product.
  Mutually exclusive with `--target-resolution` / `--offset`. The reference
  must be coarser-or-equal to the input (near-equal counts as equal, so a
  half-cell offset at the same nominal resolution is allowed). If it is
  meaningfully finer, use `downscale --reference-grid` instead.
- `--target-resolution` — target grid spacing in degrees (with `--offset`).
- `--offset` — grid offset in degrees; target points fall at
  `offset + k*resolution` (with `--target-resolution`).
- `--method` — `linear` (default) interpolates at each target point.
  `conservative` is the area-weighted mean of the source cells inside each
  target cell; use it when the target is much coarser than the input
  (fine obs onto a forecast grid before `verify`).
- `--min-fraction` — with `--method conservative`: the least fraction of a
  target cell's area that must have finite source data. Default **1.0**:
  one missing source cell (a NaN day at a station-sparse pixel, a lake or
  ocean mask at the coast) makes the whole target cell NaN, so no target
  value is built from part of its area. Lowering it (e.g. `0.9`) averages
  only the finite part — a deliberate choice, not a default. The part of a
  target cell that lies past the input's edge counts as missing too, so at
  the default an edge cell that hangs off the input is NaN rather than the
  mean of its covered part.
- `--variable`, `-v` — restrict to a single data variable. Default: process all.

### Longitude convention

Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before the target
axis is built (input and `--reference-grid`), so a global grid stored in the
`[0, 360]` convention does not produce a target axis spanning the entire
globe when only a sub-region is wanted. Inputs already in `[-180, 180]` pass
through unchanged.

### Output

Same shape as input except the lat/lon dims are replaced by the target grid.
Non-spatial dims (`number`, `step`, `time`) and CF metadata on lat/lon and
data variables are preserved. Data variables without both lat and lon (e.g.
a per-time station count) pass through unchanged. With `linear`, target
points outside the input's span (a `--reference-grid` row half a cell past
the edge) are NaN; nothing is extrapolated.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# Match IMERG's exact 0.1° coordinates (avoids float mismatch on difference).
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/forecast.zarr -o /tmp/forecast_on_imerg.zarr \
    --reference-grid /tmp/imerg.zarr
```

```bash
# CHIRPS 0.05° obs onto a 1.5° forecast grid before verify: area mean, not a point sample.
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/chirps.zarr -o /tmp/chirps_on_s2s.zarr \
    --reference-grid /tmp/s2s.zarr --method conservative
```

```bash
# Onto sheerwater's global0_25 alignment (no reference Zarr).
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/imerg.zarr -o /tmp/imerg_p25.zarr \
    --target-resolution 0.25 --offset 0.0
```

```bash
# Onto sheerwater's global0_1 alignment (no reference Zarr).
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/chirps.zarr -o /tmp/chirps_p1.zarr \
    --target-resolution 0.1 --offset 0.05
```

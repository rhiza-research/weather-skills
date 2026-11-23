---
name: mask
description: "Set a weather-skills Zarr's values to NaN wherever other Zarrs have no value (--valid-in) or a 0/1 mask is 0 (--true-in); every other value is unchanged. Works on gridded and station datasets. Use to keep only the days and points where every source has data before comparing products, or to blank out cells or dates a 0/1 mask (an indicator output, a land mask) rules out. To cut to a region's boundary, use clip-region."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/mask.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# mask

Keeps a value only where every reference allows it, and sets the rest to NaN.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/mask.py -i IN.zarr -o OUT.zarr \
  [--valid-in REF.zarr ...] [--true-in MASK.zarr ...] [--variable VAR]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--valid-in` — repeatable: keep a point only where this Zarr has a value. A value of 0 counts as a value.
- `--true-in` — repeatable 0/1 mask, such as an `indicator` output: keep a point only where it is 1.
- `--variable`, `-v` — data variable to mask. Default: every data variable. In a reference holding several variables, the one with this name is read.
- `--output`, `-o` — output Zarr.

Every `--valid-in` and `--true-in` must hold for a point to be kept.

### Output
The input with the removed values set to NaN; dims, coords, and attributes unchanged. Integer variables become float. Prints, for each variable, how many values it set to NaN.

### Notes
- Each reference must hold one data variable, or one named `--variable`; `drop` the others first.
- Dims match by name, and labels must match exactly. A label the reference lacks counts as invalid, so a reference with fewer dates or stations cuts the output to its own.
- A reference without some of the input's dims holds along them: a land mask with no `time` applies to every date.
- A reference with dims the input lacks is refused. Relabel a forecast's leads with `step-to-time`, or sample a grid at stations with `point-value`, first.
- Exits 2 when no reference is given, `--variable` does not exist, a reference has several variables or extra dims, or a variable to mask lacks a reference's dims. Exits 1 when a reference keeps no point of the input.

### Example
Keep only the station-days where TAHMO and all four products have rain values, for each product in turn:
```bash
for p in tahmo lr nrt chirps imerg; do
  uv run ${CLAUDE_SKILL_DIR}/scripts/mask.py -i $p.zarr -o masked_$p.zarr \
    --valid-in tahmo.zarr --valid-in lr.zarr --valid-in nrt.zarr \
    --valid-in chirps.zarr --valid-in imerg.zarr
done
```
Every masked file then has the same days and stations, so scores, quantiles and extremes compare like with like.

---
name: clip-values
description: "Limit the values of a weather-skills Zarr's data variables to a range: anything below --min becomes --min and anything above --max becomes --max; NaN stays NaN. Use to remove small negative rainfall left by forecasts or downscaling, or to cap unrealistic extremes."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/clip_values.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# clip-values

Sets every value below `--min` to `--min` and every value above `--max` to `--max`, and writes the rest unchanged.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/clip_values.py -i IN.zarr -o OUT.zarr [--variable VAR] [--min MIN] [--max MAX]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--variable`, `-v` — data variable to clip. Default: every data variable.
- `--min` — lower limit, in the variable's own units.
- `--max` — upper limit, in the variable's own units.
- `--output`, `-o` — output Zarr.

### Output
The input with the clipped values; dims, coords, and attributes unchanged.

### Notes
- Pass at least one of `--min` and `--max`.
- Limits are in the variable's units: check them with inspect-zarr, or convert with unit-convert first.
- Exits 2 when neither limit is given, `--min` is greater than `--max`, or `--variable` does not exist.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/clip_values.py -i ifs_precip.zarr -o ifs_precip_clipped.zarr --variable precip --min 0
```
Sets the small negative precipitation rates left by the forecast to 0 mm day-1.

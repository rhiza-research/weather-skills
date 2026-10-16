---
name: rename
description: "Rename one data variable in a weather-skills Zarr, keeping its values and attributes and passing everything else through unchanged. Use when two datasets carry the same quantity under different variable names and must match before they are combined or compared."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/rename.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# rename

Gives one data variable a new name.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/rename.py -i IN.zarr -o OUT.zarr --to-name NAME [-v NAME]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--to-name` — the new name. Must be non-empty and contain no `/`.
- `--variable`, `-v` — the data variable to rename. Default: the input's only data variable.

### Output
The input with that variable renamed; its values and attrs, and all other variables, coords, dims, and attrs, are unchanged.

### Notes
- Only data variables are renamed; a coordinate or dim name (`latitude`, `time`) exits 2.
- Exits 2 when `--to-name` already names another variable, coordinate, or dim, or when `-v` is omitted and the input has more than one data variable.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/rename.py -i imerg_kenya.zarr -o imerg_kenya_precip.zarr -v precipitation_surface --to-name precip
```
Writes the IMERG Kenya dataset with its rainfall named `precip`, matching a CHIRPS dataset.

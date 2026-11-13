---
name: drop
description: "Remove named data variables or coordinates from a weather-skills Zarr, along with any dim no data variable spans afterwards. Use to strip leftovers that stop two datasets from lining up, or to keep only the variables a later step needs."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/drop.py *)
metadata:
  version: "0.0.7"
  catalog-group: transforms
---

# drop

Removes the named data variables and coordinates, then any dim that no remaining data variable spans, and writes the rest unchanged.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/drop.py -i IN.zarr -o OUT.zarr --name NAME [--name NAME ...]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--name` — a data variable or coordinate; repeat for several. A dim's index coordinate can be named only when no remaining data variable spans that dim. Required.
- `--output`, `-o` — output Zarr.

### Output
The input without the named variables and without dims left with no data variable (stderr names them); attributes pass through unchanged.

### Notes
- A coordinate's CF `bounds` variable is not dropped with it; list it with `--name` too.
- To remove a dimension that data variables still span, pick one entry with select or reduce over it with summarize-dim. Dropping the last variables on a dim (say a pass-through `precip_std` after summarize-dim -v `precip_avg`) removes the dim with them.
- Exits 2 when a name does not exist (the message lists what does), names the index of a dim a data variable still spans, or would leave no data variables.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/drop.py -i ifs.zarr -o ifs_clean.zarr --name valid_time --name surface
```
Writes the IFS fetch without its scalar `valid_time` and `surface` coordinates, so it lines up with an IMERG dataset that lacks them.

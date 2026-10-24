---
name: merge
description: "Combine the variables of two or more weather-skills Zarrs into one dataset, keeping the coordinate values they share. Use to put related quantities side by side, e.g. a climate index and rainfall, to compare or plot one against the other."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/merge.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# merge

Puts the variables of several Zarrs into one dataset, matched on their shared coordinates.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/merge.py -i A.zarr -i B.zarr [-i C.zarr ...] -o OUT.zarr [--by-year]
```

### Arguments
- `--input`, `-i` — input Zarr; repeat for each.
- `--by-year` — match values by calendar year instead of exact time, e.g. September IOD with October rainfall. Each input needs at most one value per year.
- `--output`, `-o` — output Zarr.

### Output
One dataset holding every input's variables. Along each shared dim it keeps only the values all inputs have (time steps, years, grid cells); stderr says when values were dropped. With `--by-year`, `time` becomes an integer `year` dim.

### Notes
- Variable names must differ across inputs; rename one first with edit-coords `--rename OLD=NEW`.
- Grids must line up to be matched cell by cell; regrid one onto the other with coarsen `--reference-grid` first.
- To merge a forecast with observations, turn the forecast's steps into times with step-to-time first.
- Inputs whose time/step bounds differ (an instant field such as MSLP labelled `[prev, step]`, precip labelled `[step, next)`) exit 2: they describe different intervals.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/merge.py -i iod_sep.zarr -i kenya_rain_oct.zarr -o iod_rain.zarr --by-year
```
Writes one dataset with `dmi` and `precip` along `year`, one value each for every year both inputs cover.

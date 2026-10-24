---
name: select
description: "Pick entries along one dimension of a weather-skills Zarr, by integer position or by coordinate value: an ensemble member, a forecast step, a date, a station. Picking one entry removes that dimension but keeps the picked label as a scalar coordinate, so outputs from different sources line up for stacking. Works on gridded and station datasets. Use to take one member, lead, date or station out of a dataset."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py *)
metadata:
  version: "0.0.5"
  catalog-group: transforms
---

# select

Keeps chosen entries along one dim and passes everything else through unchanged.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py -i IN.zarr -o OUT.zarr --dim DIM (--index N [--index N ...] | --value V [--value V ...])
```

### Arguments
- `--input`, `-i` — input Zarr, gridded or point_obs.
- `--output`, `-o` — output Zarr.
- `--dim` — the one dim to select along.
- `--index` — integer position; repeat for several. Negative counts from the end (`-1` is last).
- `--value` — coordinate value; repeat for several. Parsed by the coord's type:
  - `datetime64`: naive ISO date or datetime (`2026-10-01`, `2026-10-01T06:00`); no timezone, `now`, or `today`.
  - cftime (model calendars such as `360_day`): `YYYY-MM-DD[THH[:MM[:SS]]]` in that calendar (`2026-02-30`).
  - `timedelta64`: pandas timedelta (`7D`, `168h`).
  - numeric: int or float (`0`, `1.5`); floats match within 1e-9.
  - string: verbatim (`TA00025`).

Pass `--index` or `--value`, not both.

### Output
- One entry: the dim is removed and the selected label stays as a scalar coordinate (e.g. `time = 2026-09-28` or `step = 7 days`, plus `valid_time` when present), so verify and plot still know which date or lead it is. Other coordinates it leaves scalar are dropped. Station identity (`station_id`, `point_id`, `name`, `latitude`, `longitude`, `country`) and `aggregation_coverage` stay as scalar coords. A calendar `aggregation_period` such as `1 month` becomes the selected bin's length (`28 day` for February).
- Several entries: the dim keeps exactly those, in the order given (`--index 2 --index 0` reverses them).
- Other dims, coords, variables, and attrs are unchanged.

### Notes
- These exit 2 with no output: an unknown or repeated `--dim`; an out-of-range or repeated position (`0` and `-4` on a size-4 dim are the same); a value not on the coord (the error lists the first few); a value matching duplicate labels; `--value` on a dim with no coordinate values. Use `--index` for the last two.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py -i ecmwf_weekly_kenya.zarr -o ecmwf_w1_kenya.zarr --dim step --index 0
```
Writes week-1 of the weekly forecast with no `step` dim, ready to stack with other models' week-1.

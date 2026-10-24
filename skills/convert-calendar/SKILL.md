---
name: convert-calendar
description: "Convert a weather-skills Zarr's time axis to another CF calendar: the standard calendar of observations, or a climate-model calendar such as noleap (no Feb 29) or 360_day (twelve 30-day months). Use to put two datasets on one calendar before comparing them, e.g. a noleap model run against standard-calendar observations."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# convert-calendar

Re-expresses the dates on the time axis in a target calendar, using xarray's `Dataset.convert_calendar`.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py -i IN.zarr -o OUT.zarr --calendar NAME [--align-on date|year] [--time-dim NAME]
```

### Arguments
- `--input`, `-i` — input Zarr with a time axis of dates.
- `--output`, `-o` — output Zarr.
- `--calendar` — target: `standard`, `gregorian`, `proleptic_gregorian`, `noleap`, `365_day`, `360_day`, `all_leap`, `366_day`, or `julian`.
- `--align-on` — how to map dates when the source or target is `360_day` (required then, ignored otherwise):
  - `date` keeps month and day and drops dates that do not exist (Feb 29/30, the 31st). Suits data coarser than daily.
  - `year` maps by position in the year. To `360_day` it drops 5–6 days a year; from `360_day` it leaves 5–6 days a year with no sample. Suits daily or sub-daily data.
- `--time-dim` — the time dim. Default: the CF `T` axis, else the dim named `time`.

### Output
The same variables, dims, coords, and attrs, with the time axis in the target calendar: `datetime64` for `standard`, `gregorian`, and `proleptic_gregorian`, `cftime` objects for the others. CF time bounds (named by the time coord's `bounds` attr) move with their labels and keep each cell's width.

### Notes
- Dates that do not exist in the target calendar (Feb 29 going to `noleap`) are dropped and listed on stderr; nothing is filled in. If every date would be dropped, it exits 2.
- The time axis must hold dates. A forecast whose only time axis is the `step` lead exits 2; give it valid times first.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py -i cmip6_tas_kenya.zarr -o cmip6_tas_kenya_std.zarr --calendar standard
```
Writes the noleap CMIP6 temperature run on a standard `datetime64` axis, ready to line up with ERA5.

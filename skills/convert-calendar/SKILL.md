---
name: convert-calendar
description: Convert a weather-skills standard dataset Zarr's time axis to a target CF calendar by wrapping xarray's Dataset.convert_calendar. Use to align two datasets onto a common calendar before comparison — e.g. converting a model-calendar forecast (noleap/360_day) to the standard calendar of observations. Converting to a standard calendar yields a datetime64 axis; converting to a model calendar yields a cftime axis. Dates not representable in the target calendar are dropped.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# convert-calendar

Convert the time axis of a weather-skills standard dataset Zarr to a target CF calendar. CF
datasets may use different calendars — the standard (proleptic Gregorian)
calendar of observations, or a model calendar such as `noleap` (no Feb 29) or
`360_day` (twelve 30-day months). The same wall-clock date maps to a different
day-count per calendar, and some dates don't exist across calendars, so two
datasets on different calendars cannot be compared directly. This skill wraps
xarray's `Dataset.convert_calendar` to put a dataset on a chosen calendar so it
can be aligned with another before comparison. It reads any valid CF input
(including a non-standard model calendar) and emits valid CF.

Converting **to a standard calendar** (`standard`, `proleptic_gregorian`) yields
a `datetime64` time axis. Converting **to a model calendar** (`noleap`,
`360_day`, `all_leap`, `julian`, ...) yields an object-dtype `cftime` axis.

`--align-on` is **required** whenever the source or target calendar is
`360_day`. Dates not representable in the target calendar (e.g. Feb 29 when
converting to `noleap`) are **dropped** from the output and listed on stderr;
missing dates are never filled. If every date would be dropped, the skill exits
with an error instead of writing an empty store.

## When to use

- Aligning a model-calendar forecast (`noleap`/`360_day`) to the standard
  calendar of an observation dataset so the two can be compared.
- Putting two datasets on a common CF calendar before any calendar-sensitive
  step.

Not for: resampling or rolling up the time axis into fixed windows (daily,
weekly, monthly) — that is `aggregate-temporal`. Not for comparing or plotting
two datasets. This skill changes only the calendar of
the time axis; it does not resample, reduce, or render.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py --input <in.zarr> --output <out.zarr> \
    --calendar NAME \
    [--time-dim NAME] [--align-on date|year]
```

### Arguments
- `--input`, `-i` — input Zarr (any standard dataset with a wall-clock `time`
  dim). A forecast whose only time dim is `step` (lead) is refused: run
  `step-to-time` first.
- `--output`, `-o` — output Zarr.
- `--calendar` — target CF calendar name (`standard`, `proleptic_gregorian`,
  `noleap`, `360_day`, `all_leap`, `julian`, ...).
- `--time-dim` — name of the time dim when it is not auto-detectable. Defaults
  to the CF "T" axis (cf-xarray), then a dim named `time`. Must hold dates.
- `--align-on` — `date` or `year`. **Required** when the source or target
  calendar is `360_day`. `year` translates dates by relative position in the
  year (best for daily/sub-daily data; to `360_day` it drops 5–6 days a year,
  from `360_day` it leaves 5–6 days a year with no sample); `date` conserves
  month/day and drops invalid dates such as Feb 29/30 or the 31st (best for
  coarser-than-daily data). Ignored for conversions not involving `360_day`.

### Output

Same variables, dims, and coords as the input, with the time axis re-expressed
on the target calendar. Converting to a standard calendar gives a
`datetime64` axis; converting to a model calendar gives a `cftime` axis. Dates
not representable in the target calendar are dropped (so the time axis may be
shorter than the input's) and listed on stderr. CF time bounds (the variable
named by the time coord's `bounds` attr, e.g. `time_bounds`) move with their
labels onto the target calendar, keeping each cell's exact width. CF coordinate
and data-variable attributes (`data_interval`, `aggregation_period`,
`aggregation_coverage`) are carried over unchanged.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# Convert a noleap forecast to the standard calendar so it can be compared
# against standard-calendar observations.
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py \
    -i /tmp/forecast_noleap.zarr -o /tmp/forecast_standard.zarr \
    --calendar standard
```

```bash
# Convert standard-calendar observations onto a 360_day model calendar.
# --align-on is required because 360_day is involved.
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py \
    -i /tmp/obs.zarr -o /tmp/obs_360.zarr \
    --calendar 360_day --align-on year
```

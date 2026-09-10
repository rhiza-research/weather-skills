---
name: day-of-year
description: Extract calendar day-of-year (1-366) from a datetime64 data variable, replacing it with an integer that can be averaged and thresholded -- a raw date/duration cannot. Use downstream of onset-date (or any other date-producing skill) before summarize-dim or exceedance-probability, since averaging a raw datetime64 value is not meaningful. NOT needed to map an onset result: plot-onset takes onset-date's output directly and derives this itself.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/day_of_year.py *)
metadata:
  catalog-group: transforms
---

# day-of-year

Source-agnostic day-of-year extraction. For each selected data variable,
replaces it with its calendar day of year (via xarray's `.dt.dayofyear`
accessor) under a new `VAR_dayofyear` name, keeping the exact same shape and
dims — this is element-wise, not a reduction over any dim. `NaT` entries
become `NaN`. Data variables that aren't selected pass through untouched.

Only `datetime64`-typed variables qualify. A lead-time/duration
(`timedelta64`) variable — e.g. `onset-date`'s output when its input carried
a `step` axis rather than absolute `time` — has no calendar day of year to
read; run `step-to-time` upstream of the date-producing skill first so the
date ends up absolute.

## When to use

**Not for onset maps.** To map an onset result, use `plot-onset`, which
takes `onset-date`'s output directly and derives the day-of-year, mean, and
member coverage itself. Running this skill first is unnecessary there and
throws away the coverage half of the figure.

Use it when an onset result needs to become a plain number:

- `summarize-dim --dim number` for a mean/std onset day across ensemble
  members (averaging raw dates directly is meaningless; averaging their
  day-of-year is a well-defined circular-ish approximation for a single
  season). **Caveat:** `onset-date`'s `NaT`s become `NaN` here, and
  `summarize-dim --method mean` skips them — so a mean is averaged only
  over members that found an onset, and a low-agreement cell's mean looks
  just as confident as a high-agreement one. See `onset-date`'s SKILL.md
  for the exceedance-probability recipe that gives a companion
  member-coverage figure, and report it alongside any mean onset.
- `exceedance-probability` for "probability onset falls before day N."
- `plot` on a *non-onset* datetime64 field, which needs a numeric variable
  for its colorbar range — it computes `vmin`/`vmax` via `float(da.max())`,
  which raises on a date/duration dtype.
- Any other datetime64 data variable that needs to become a plain integer
  for the same reasons.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/day_of_year.py \
    --input <in.zarr> --output <out.zarr> \
    [--variable VAR ...]
```

### Arguments

- `--input`, `-i` — input Zarr (any).
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — repeatable; restricts the computation to the named
  data variable(s). Each name must be a data variable of the input and must
  be `datetime64`-typed; a `timedelta64` (lead-time) or other non-date
  variable exits non-zero with a pointer to `step-to-time`. Default (unset)
  computes over every `datetime64`-typed data variable; if none exist, exits
  non-zero. Unselected or untouched data variables pass through unchanged (a
  stderr note lists them).

### Output

Each selected `VAR` becomes `VAR_dayofyear`; `VAR` itself is removed. Dims
and shape are unchanged (no dim is collapsed). `units` is set to the CF
dimensionless-count convention `"1"`; `standard_name` is `None` (CF has no
entry for "day of year" to verify against); `long_name`/`GRIB_name` name the
source variable.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: the input's
chain plus an entry for this run, `{skill, version, args, input}` (`version`
is the value printed by `--help`). Inspect a written output's lineage with
the `provenance` skill. There is no cache: every run recomputes and rewrites
`--output`, even against an unchanged input with identical flags.

## Examples

```bash
# Onset date -> day of year, then mean/std across the ensemble.
uv run ${CLAUDE_SKILL_DIR}/../onset-date/scripts/onset_date.py \
    -i /tmp/ecmwf.zarr -o /tmp/onset.zarr --definition ICPAC --variable tp
uv run ${CLAUDE_SKILL_DIR}/scripts/day_of_year.py \
    -i /tmp/onset.zarr -o /tmp/onset_doy.zarr
uv run ${CLAUDE_SKILL_DIR}/../summarize-dim/scripts/summarize_dim.py \
    -i /tmp/onset_doy.zarr -o /tmp/onset_doy_mean.zarr --dim number --method mean
```

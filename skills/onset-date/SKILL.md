---
name: onset-date
description: Compute the rainy season onset date along a time/step axis, per one of two selectable definitions -- ICPAC's wet-spell-then-no-dry-spell criterion, or the Climate Hazards Center's two-window cumulative-rainfall criterion (CHC_start_grow_season). Use whenever a dataset needs a per-gridpoint (or per-ensemble-member) onset date derived from a daily rainfall accumulation series. The output is a raw date/duration -- run the day-of-year skill on it before plot, summarize-dim, or exceedance-probability, since none of those handle a raw datetime64/timedelta64 value directly (plot errors outright on one).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py *)
metadata:
  catalog-group: transforms
---

# onset-date

Source-agnostic rainy-season-onset date along the time-like dim. For each
selected data variable `VAR`, finds the first day along `time` (or a
lead-time dim such as `step`) satisfying an onset criterion, and writes that
day's own coordinate value to a new `onset_VAR_date` variable, replacing
`VAR`. Data variables that don't carry the time dim pass through untouched.

Two onset definitions are available via `--definition`:

- `ICPAC` — a wet spell (`--wet-spell-days` consecutive days totaling more
  than `--wet-spell-thresh`) qualifies as onset only if no dry spell
  (`--dry-spell-days` or more consecutive days below `--dry-spell-thresh`)
  occurs within the following `--search-days` days.
- `CHC_start_grow_season` — the Climate Hazards Center definition: the first
  day where the following `--period1-days` days accumulate at least
  `--period1-thresh`, and the `--period2-days` days immediately after that
  accumulate more than `--period2-thresh`. No dry-spell check — the
  confirmation window's own total is the only follow-through condition.

## When to use

- Agromet-style onset detection on daily rainfall: `--definition ICPAC` with
  the classic 20mm/3-day wet spell and a 7-day dry-spell disqualifier over a
  21-day search window (the defaults).
- A simpler two-window accumulation check: `--definition CHC_start_grow_season`.
- Per-ensemble-member onset spread: run against a forecast with a `number`
  dim, convert the resulting date to a comparable scalar with `day-of-year`
  (raw dates can't be averaged meaningfully), then feed that into
  `summarize-dim --dim number` for a mean/std onset day, or
  `exceedance-probability` for "probability onset falls before day N."
- Absolute onset *dates* (not elapsed lead time): run `step-to-time` first so
  the time dim already carries `datetime64` values before this skill runs —
  this skill does not do that conversion itself. `day-of-year` (chained
  after this skill) also requires an absolute `datetime64` onset date, not
  an elapsed-lead-time `timedelta64` one.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py \
    --input <in.zarr> --output <out.zarr> \
    --definition ICPAC|CHC_start_grow_season \
    [--variable VAR ...] [--time-dim DIM] \
    [--wet-spell-thresh MM] [--wet-spell-days N] \
    [--dry-spell-thresh MM] [--dry-spell-days N] [--search-days N] \
    [--period1-days N] [--period1-thresh MM] \
    [--period2-days N] [--period2-thresh MM]
```

### Arguments

- `--input`, `-i` — input Zarr (any).
- `--output`, `-o` — output Zarr.
- `--definition` — `ICPAC` or `CHC_start_grow_season`. Required.
- `--variable`, `-v` — repeatable; restricts the computation to the named
  data variable(s). Each name must be a data variable of the input and must
  carry the time dim; violations exit non-zero. Default (unset) computes
  over every data variable carrying the time dim. Unselected or untouched
  data variables pass through unchanged (a stderr note lists them).
- `--time-dim` — name of the time-like dim when not auto-detectable.

ICPAC-only parameters (ignored under `--definition CHC_start_grow_season`):

- `--wet-spell-thresh` — total rainfall a wet spell must exceed, in the
  variable's own units. Default `20.0`.
- `--wet-spell-days` — consecutive days summed for the wet-spell check.
  Default `3`.
- `--dry-spell-thresh` — a day below this rainfall counts as dry. Default `1.0`.
- `--dry-spell-days` — a dry run this long or longer disqualifies the onset.
  Default `7`.
- `--search-days` — window, counted from the wet spell's first day, searched
  for a disqualifying dry spell. Default `21`.

CHC_start_grow_season-only parameters (ignored under `--definition ICPAC`):

- `--period1-days` — length of the first accumulation window. Default `10`.
- `--period1-thresh` — the first window must accumulate at least this much.
  Default `20.0`.
- `--period2-days` — length of the confirmation window immediately after the
  first. Default `20`.
- `--period2-thresh` — the confirmation window must accumulate more than
  this much. Default `20.0`.

### Time-dim detection

Without `--time-dim`, the skill first tries the dim ontology's time
detection (CF "T" axis, then a literal `time` dim). When that finds nothing —
a classic forecast dataset, where `time` is a scalar init-date coordinate
rather than a dim — it falls back to whichever dim the ontology aliases to
the lead-time axis (e.g. `step`) and prints a note naming the dim it picked.

### NaN handling and units

Any missing value (`NaN`) inside a candidate's wet/dry-spell (or period1/
period2) window marks that candidate disqualified for that gridpoint/member;
a series with no qualifying, uncontaminated onset anywhere returns `NaT` for
that element rather than reporting a possibly-unreliable day.

No unit conversion happens in this skill; use `unit-convert` upstream if the
thresholds need to be expressed in the variable's native units. The output
variable's attrs are built fresh rather than carried over from the source
variable: the source's `standard_name`/`long_name`/`units` describe the
input rainfall quantity, not this derived date. `long_name`/`GRIB_name` are
both set to a compact descriptive label naming the definition and its
concrete parameters (e.g. `"tp onset date (ICPAC: 20.0 mm/3d, dry<1.0 mm for
7d in 21d)"`), and `description` carries the full prose definition actually
used. `standard_name` is set to `None` explicitly (CF has no entry for
"rainy season onset date" to verify against). No `units` attr is set at
all — the result is genuinely `timedelta64`/`datetime64`-typed, and
xarray's CF time encoder insists on owning that attr itself; a `units`
string present at write time (even a healed-in one) raises.

This is also why the result lands on `onset_VAR_date` rather than
`VAR_onset_date` or `onset_date_VAR`: `weather_skills_core` classifies a
variable's physical kind (and whether it then requires `units`) by whether
its name *starts or ends with* a short hint like `tp`, `pr`, `t2m`, `tas` —
exactly the source variable names this skill is typically run on. Either
prefix or suffix placement would put that hint at a boundary of the new name
and misclassify the date output as precip/temp (which *does* require
units), making it unreadable as `--input` to any other skill, including
`day-of-year`. Sandwiching `VAR` between fixed, non-hint text on both sides
avoids that regardless of what `VAR` is named.

### Output

Each selected `VAR` becomes `onset_VAR_date`, with the time dim collapsed;
`VAR` itself is removed. The output dtype matches the time dim's own
coordinate dtype: a duration (`timedelta64`) if the dim is a lead-time axis
like `step`, or an absolute date (`datetime64`) if the dim is already `time`
(i.e. `step-to-time` ran upstream). The collapsed dim disappears from the
output (along with its coordinates) once no data variable carries it; a dim
still carried by a pass-through variable stays. Remaining dims (e.g.
`number`, `latitude`, `longitude`), coords, and pass-through variables are
unchanged.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: the input's
chain plus an entry for this run, `{skill, version, args, input}` (`version`
is the value printed by `--help`). Inspect a written output's lineage with
the `provenance` skill. There is no cache: every run recomputes and rewrites
`--output`, even against an unchanged input with identical flags.

## Examples

```bash
# ICPAC onset (wet spell + dry-spell check) on an ECMWF S2S forecast, step axis.
uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py \
    -i /tmp/ecmwf.zarr -o /tmp/ecmwf_onset.zarr \
    --definition ICPAC --variable tp
```

```bash
# CHC_start_grow_season onset, custom windows.
uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py \
    -i /tmp/chirps.zarr -o /tmp/chirps_onset.zarr \
    --definition CHC_start_grow_season \
    --period1-days 10 --period1-thresh 20 --period2-days 20 --period2-thresh 20
```

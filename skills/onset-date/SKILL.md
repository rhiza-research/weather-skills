---
name: onset-date
description: Compute the rainy season onset date along a time/step axis, per a definition from the onset-definition registry (--definition-ref, e.g. icpac-onset, agrhymet-sos-rolling, moron-robertson-2014; every output records the definition id, content hash and any overrides) or one of three legacy names -- ICPAC's wet-spell-then-no-dry-spell criterion, the Climate Hazards Center's two-window cumulative-rainfall criterion (CHC_start_grow_season), or Moron-Robertson's all-wet window over a per-cell climatological threshold (Moron_Robertson_2014). Use whenever a dataset needs a per-gridpoint (or per-ensemble-member) onset date derived from a daily rainfall accumulation series. To MAP the result, use plot-onset, which takes this output directly and shows mean onset and member agreement together. The output is otherwise a raw date/duration -- run the day-of-year skill on it before summarize-dim or exceedance-probability, since neither handles a raw datetime64/timedelta64 value directly.
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

Definitions are selected by registry id with `--definition-ref` (see
[Onset-definition registry](#onset-definition-registry-and-provenance)), or
by one of three legacy names with `--definition`, which run the same three
kernels:

- `ICPAC` — a wet spell (`--wet-spell-days` consecutive days totaling more
  than `--wet-spell-thresh`) qualifies as onset only if no dry spell
  (`--dry-spell-days` or more consecutive days below `--dry-spell-thresh`)
  occurs within the following `--search-days` days.
- `CHC_start_grow_season` — the Climate Hazards Center definition: the first
  day where the following `--period1-days` days accumulate at least
  `--period1-thresh`, and the `--period2-days` days immediately after that
  accumulate more than `--period2-thresh`. No dry-spell check — the
  confirmation window's own total is the only follow-through condition.
- `Moron_Robertson_2014` — Moron & Robertson (2014): the first day `d` where every
  day of `[d, d + --mr-window-days)` is wet (at least `--mr-wet-day-thresh`)
  and that window's total exceeds a per-cell threshold — canonically the
  local climatological wet-spell amount, supplied as `--mr-thresh-field` (or
  one value for every cell, `--mr-thresh`). The candidate is vetoed if the
  `--mr-follow-days` days after the trigger window contain a dry spell
  (`--mr-veto`); a vetoed candidate does not end the search — the next
  triggering day is tried.

  Not the same as Sheerwater's `moron_and_robertson_onset` (registry id
  `sheerwater-moron-robertson-onset`). That is a simplification: one fixed 38 mm
  5-day threshold instead of the per-cell climatology, no all-days-wet test, and
  "next 10 days total more than 5 mm" instead of the 30-day dry-spell check. Hence
  the `_2014` in this definition's name.

## When to use

- Agromet-style onset detection on daily rainfall: `--definition ICPAC` with
  the classic 20mm/3-day wet spell and a 7-day dry-spell disqualifier over a
  21-day search window (the defaults).
- A simpler two-window accumulation check: `--definition CHC_start_grow_season`.
- Onset relative to local climatology, as in monsoon onset forecasting:
  `--definition Moron_Robertson_2014` with a per-cell threshold field. The
  defaults are the original definition (5-day trigger, 30-day follow-up,
  no 10-day window below 5 mm). The Ethiopia/ICPAC-style variant is
  `--mr-veto consecutive_dry --mr-follow-days 21` (7 dry days). To ignore
  rain before a climatological onset date (e.g. the monsoon's arrival over
  Kerala in early June for India), add `--mr-search-start MM-DD`.
- Mapping the result: use `plot-onset`, which takes this skill's output
  directly and renders mean onset date and per-cell member agreement in one
  figure. Do not build that by hand, and do not use `plot` (it errors on a
  date dtype).
- Per-ensemble-member onset spread as *numbers* rather than a map: convert
  the resulting date to a comparable scalar with `day-of-year` (raw dates
  can't be averaged meaningfully), then feed that into `summarize-dim --dim
  number` for a mean/std onset day, or `exceedance-probability` for
  "probability onset falls before day N." See the caveat below before
  reporting a mean this way.
- Absolute onset *dates* (not elapsed lead time): run `step-to-time` first so
  the time dim already carries `datetime64` values before this skill runs —
  this skill does not do that conversion itself. `day-of-year` (chained
  after this skill) also requires an absolute `datetime64` onset date, not
  an elapsed-lead-time `timedelta64` one.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py \
    --input <in.zarr> --output <out.zarr> \
    (--definition-ref REGISTRY_ID [--waive-field FIELD ...] \
     | --definition ICPAC|CHC_start_grow_season|Moron_Robertson_2014) \
    [--variable VAR ...] [--time-dim DIM] \
    [--wet-spell-thresh MM] [--wet-spell-days N] \
    [--dry-spell-thresh MM] [--dry-spell-days N] [--search-days N] \
    [--period1-days N] [--period1-thresh MM] \
    [--period2-days N] [--period2-thresh MM] \
    [--mr-thresh MM | --mr-thresh-field <thr.zarr> [--mr-thresh-field-var V]] \
    [--mr-window-days N] [--mr-wet-day-thresh MM] [--mr-follow-days N] \
    [--mr-veto window_sum|consecutive_dry] \
    [--mr-sum-window-days N] [--mr-sum-thresh MM] \
    [--mr-dry-spell-days N] [--mr-dry-day-thresh MM] \
    [--mr-search-start MM-DD] [--mr-reject-short-followup]
```

### Arguments

- `--input`, `-i` — input Zarr (any).
- `--output`, `-o` — output Zarr.
- `--definition-ref` — an id from the onset-definition registry
  (`references/onset_definitions.toml`), e.g. `icpac-onset`,
  `agrhymet-sos-rolling`, `moron-robertson-2014`, `uchicago-ethiopia-2026`,
  `moron-robertson-india-operational`. An unknown id is refused with the list
  of known ids. Exactly one of `--definition-ref` / `--definition` is required.
- `--waive-field FIELD` — repeatable, `--definition-ref` only: run without a
  registry field the kernel cannot reproduce (see the refusal rule below).
- `--definition` — legacy name: `ICPAC`, `CHC_start_grow_season` or
  `Moron_Robertson_2014`. Kept for backward compatibility with its old defaults.
- `--variable`, `-v` — repeatable; restricts the computation to the named
  data variable(s). Each name must be a data variable of the input and must
  carry the time dim; violations exit non-zero. Default (unset) computes
  over every data variable carrying the time dim. Unselected or untouched
  data variables pass through unchanged (a stderr note lists them).
- `--time-dim` — name of the time-like dim when not auto-detectable.

Kernel parameters. The defaults below apply under `--definition`; under
`--definition-ref` every one of them comes from the registry entry, and a
flag given explicitly still wins but is recorded as an override. Under
`--definition-ref`, a flag belonging to a different kernel is refused.

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

Moron_Robertson_2014-only parameters (ignored under the other definitions, except
that `--mr-thresh`, `--mr-thresh-field`, `--mr-thresh-field-var` and
`--mr-search-start` are refused there rather than silently ignored):

- `--mr-thresh` / `--mr-thresh-field` — the trigger threshold: the trigger
  window's total must exceed it. Exactly one is required; there is **no
  default**, and the skill will not compute a climatology for you.
  `--mr-thresh` is one value for every cell. `--mr-thresh-field` is a Zarr
  holding one value per cell (e.g. the mean 5-day wet-spell total from a
  reference period); its dims must be a subset of the variable's non-time
  dims with identical coordinates (a field without `number` applies to
  every member), and its `units`, if set, must match the variable's. A
  mismatched grid or unit is refused, not regridded. A `NaN` threshold cell
  yields `NaT`. `--mr-thresh-field-var` picks the variable when the Zarr
  holds more than one.
- `--mr-window-days` — trigger window length; every day in it must be wet.
  Default `5`.
- `--mr-wet-day-thresh` — a day with at least this rainfall is wet.
  Default `1.0`.
- `--mr-follow-days` — days after the trigger window searched for a dry
  spell; `0` disables the veto. Default `30`.
- `--mr-veto` — `window_sum` (default; the original definition): a
  `--mr-sum-window-days`-day window lying wholly inside the follow-up totals
  less than `--mr-sum-thresh` (defaults `10`, `5.0`). `consecutive_dry`: a
  run of `--mr-dry-spell-days` (default `7`) or more days below
  `--mr-dry-day-thresh` (default: `--mr-wet-day-thresh`) *starts* inside the
  follow-up (it may run past its end).
- `--mr-search-start MM-DD` — skip candidate onset days before this date in
  the year of the first time step (search the whole series if it starts
  later). Needs an absolute `time` dim; on a lead-time axis, run
  `step-to-time` first.
- `--mr-reject-short-followup` — reject a candidate whose follow-up period
  runs past the end of the series. By default the veto is checked over the
  days that remain, and a candidate is rejected only if a dry spell is
  actually found in them — relevant for forecasts, whose horizon is often
  shorter than trigger plus follow-up.

The `Moron_Robertson_2014` search reproduces the reference implementation from
the University of Chicago monsoon-onset work (`find_onset` in
`github.com/amarchakitus/onset_blending`, MIT) day-for-day on gap-free
series; the tests carry that function as an oracle. The one intended difference is missing data: here any
`NaN` in a candidate's trigger or follow-up window disqualifies it (see
below), where the reference ignores a missing follow-up day.

### Onset-definition registry and provenance

`references/onset_definitions.toml` is a byte-identical copy of the
repository's `registry/onset_definitions.toml` (refresh it with
`python tools/sync_definitions.py`; a test fails if the two drift). Each entry
states one definition once, with its status (`canonical`, `variant`,
`candidate`) and source.

`--definition-ref` picks the kernel from the entry's structure (an all-wet
trigger over a per-cell threshold: Moron_Robertson_2014; a confirmation window:
CHC; a consecutive-dry veto: ICPAC) and fills every parameter from it:

| Registry field | ICPAC kernel | CHC kernel | Moron_Robertson_2014 kernel |
|---|---|---|---|
| `trigger.window_days` | `--wet-spell-days` | `--period1-days` | `--mr-window-days` |
| `trigger.total_mm` / `total_op` | `--wet-spell-thresh`, `>` or `>=` | `--period1-thresh`, `>` or `>=` | must be per-cell: `--mr-thresh-field` (or `--mr-thresh`, an override); op must be `>` |
| `trigger.wet_day_mm` / `wet_day_op` | — | — | `--mr-wet-day-thresh`; op must be `>=` |
| `confirm.window_days` / `total_mm` / `total_op` | — | `--period2-days` / `--period2-thresh`, `>` or `>=` | — |
| `confirm.after_days` | — | must equal `trigger.window_days` | — |
| `veto.mode` | must be `consecutive_dry` | must be `none` | `--mr-veto` (`none` = `--mr-follow-days 0`) |
| `veto.dry_days` / `dry_day_mm` | `--dry-spell-days` / `--dry-spell-thresh` | — | `--mr-dry-spell-days` / `--mr-dry-day-thresh` |
| `veto.window_days` / `window_total_mm` | — | — | `--mr-sum-window-days` / `--mr-sum-thresh` |
| `veto.follow_days` | `--search-days` | — | `--mr-follow-days` |
| `veto.follow_anchor` | kernel: dry run wholly inside the window from the trigger's first day | — | `run_start_after_trigger_end` / `window_start_after_trigger_end` |
| `search.start` | — | — | one date: `--mr-search-start` |
| `search.window_days`, `time_basis = calendar_dekad` | — | — | — |

**Refusal rule.** A registry field the chosen kernel cannot reproduce is
refused with a usage error naming the field, never silently ignored — e.g.
`time_basis = "calendar_dekad"` (`agrhymet-sos`), `search.window_days` and a
two-season `search.start` (`icpac-onset`), and `icpac-onset`'s
`veto.follow_anchor = "run_start_after_trigger_start"` (the ICPAC kernel only
vetoes a dry run lying wholly inside the window). `--waive-field FIELD` runs
without it; the waiver is recorded as an override. Keys outside the scientific
sections (`notes`, `tunable`, `optimization`, ...) are ignored.

**Provenance.** Every output variable and the output dataset carry:

- `onset_definition_id` — the registry id (for `--definition`, the entry the
  legacy name approximates: `ICPAC` → `icpac-onset`, `CHC_start_grow_season`
  → `agrhymet-sos-rolling`, `Moron_Robertson_2014` → `moron-robertson-2014`);
- `onset_definition_hash` — the entry's content hash (first 12 hex digits of
  the SHA-256 of its `time_basis`/`trigger`/`confirm`/`veto`/`search`
  sections as sorted-key JSON), so a prose edit keeps the identity;
- `onset_definition_status` — the entry's status, or `unregistered-variant`
  when anything differs from it;
- `onset_definition_overrides` — JSON object of every differing field and
  the value actually used (`null` = not applied), `"{}"` when none. A scalar
  `--mr-thresh` records `trigger.threshold_kind: "scalar"`, since the
  registered Moron-Robertson threshold is a per-cell climatology.

**Known divergences of the legacy names.** `--definition CHC_start_grow_season`
keeps its 20 mm first-window default, but the AGRHYMET / FEWS NET start of
season is "first dekad with at least 25 mm, followed by two dekads totalling
at least 20 mm" (the registry cites Environ. Res. Lett. 2021,
doi:10.1088/1748-9326/ac15cc). A legacy CHC run therefore records
`{"trigger.total_mm": 20.0, "confirm.total_op": ">"}` and status
`unregistered-variant`; `--definition-ref agrhymet-sos-rolling` runs 25 mm
and `>=`. Changing the legacy default is a maintainer decision, not made
here. Likewise `--definition ICPAC` compares the wet-spell total with `>`
where the registry uses `>=` (a convention the source leaves unspecified),
and has no search-start or search-window limit.

### Time-dim detection

Without `--time-dim`, the skill first tries the dim ontology's time
detection (CF "T" axis, then a literal `time` dim). When that finds nothing —
a classic forecast dataset, where `time` is a scalar init-date coordinate
rather than a dim — it falls back to whichever dim the ontology aliases to
the lead-time axis (e.g. `step`) and prints a note naming the dim it picked.

### NaN handling and units

Any missing value (`NaN`) inside a candidate's wet/dry-spell (or period1/
period2, or Moron_Robertson_2014 trigger/follow-up) window marks that candidate
disqualified for that gridpoint/member;
a series with no qualifying, uncontaminated onset anywhere returns `NaT` for
that element rather than reporting a possibly-unreliable day.

No unit conversion happens in this skill; use `unit-convert` upstream if the
thresholds need to be expressed in the variable's native units. The output
variable's attrs are built fresh rather than carried over from the source
variable: the source's `standard_name`/`long_name`/`units` describe the
input rainfall quantity, not this derived date. `long_name`/`GRIB_name` are
both set to a compact descriptive label naming the definition and its
concrete parameters (e.g. `"tp onset date (ICPAC: >20.0 mm/3d, dry<1.0 mm for
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

### Caveat: averaging onset across ensemble members

`onset-date` returns `NaT` for any member/gridpoint that never satisfies the
rule. `summarize-dim --method mean` (like xarray's own `.mean()`) skips
missing values by default — so a cell's mean onset is averaged only over
whichever members *did* find one. A cell where just 2 of 51 members
triggered still produces a confident-looking mean, backed by almost no
members, indistinguishable in the output from a cell where 49 of 51 agreed.

**If you are making a map, `plot-onset` solves this for you** — it takes
this skill's output directly, derives the mean and the member coverage
itself, fades low-coverage cells, and annotates the member percentage over
the map. Prefer it over a hand-built mean-then-plot chain. The rest of this
section applies when you are reporting a mean onset as a number rather than
as a map.

**If you report a mean onset date (or day-of-year) computed this way, say so
explicitly** — something like: *"One caveat worth knowing before you use
this: the onset skill returns NaT for a member that never meets the rule,
and summarize-dim's mean skips missing values. So each cell's mean is
averaged only over the members that did find an onset — cells where few
members triggered give an early-looking mean backed by a handful of members.
Read it alongside a probability map, which tells you how many members that
mean rests on."*

Get that companion coverage map with `exceedance-probability`, run on the
day-of-year result (not the raw onset date) with a threshold every valid day
satisfies and none of the `NaT`-derived `NaN`s can (comparisons against
`NaN` are always false, so those members are correctly excluded from the
count):

```bash
uv run ${CLAUDE_SKILL_DIR}/../exceedance-probability/scripts/exceedance_probability.py \
    -i /tmp/onset_doy.zarr -o /tmp/onset_pct_valid.zarr \
    --dim number --threshold 1 --comparison ge
```

This yields the percentage of members that found an onset at all per cell —
report it alongside the mean, and treat a low-coverage cell's mean as
unreliable rather than as a genuinely early onset.

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
# AGRHYMET / FEWS NET start of season on rolling daily windows, by registry id.
uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py \
    -i /tmp/chirps.zarr -o /tmp/chirps_sos.zarr \
    --definition-ref agrhymet-sos-rolling
```

```bash
# CHC_start_grow_season onset, custom windows.
uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py \
    -i /tmp/chirps.zarr -o /tmp/chirps_onset.zarr \
    --definition CHC_start_grow_season \
    --period1-days 10 --period1-thresh 20 --period2-days 20 --period2-thresh 20
```

```bash
# Moron_Robertson_2014 onset on IMD daily rainfall (absolute time dim), original
# definition, per-cell climatological threshold, search from June 2.
uv run ${CLAUDE_SKILL_DIR}/scripts/onset_date.py \
    -i /tmp/imd_2026.zarr -o /tmp/imd_2026_onset.zarr \
    --definition Moron_Robertson_2014 \
    --mr-thresh-field /tmp/imd_wet_spell_clim.zarr --mr-search-start 06-02
```

## References

- Moron, V. & Robertson, A. W. (2014). Interannual variability of Indian
  summer monsoon rainfall onset date at local scale. *International Journal
  of Climatology*, 34(4), 1050-1061. — the `Moron_Robertson_2014` definition.
- Masiwal, R., Aitken, C., Marchakitus, A., et al. (2026). Decision-oriented
  benchmarking to transform AI weather forecast access: Application to the
  Indian monsoon. arXiv:2602.03767 — operational monsoon onset forecasting;
  the onset code behind that work, `github.com/amarchakitus/onset_blending`
  (MIT), is the reference this skill's `Moron_Robertson_2014` search is tested
  against.

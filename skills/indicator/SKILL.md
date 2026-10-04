---
name: indicator
description: >-
  Apply a boolean indicator to a daily weather-skills standard dataset Zarr
  (windowed precip thresholds, sequential onset rules) and optionally reduce to
  ensemble probability. Use for ICPAC/CHC rainy-season onset, ad-hoc rules like
  >25 mm in 8 days or <9 mm in 10 days, wet/dry spells, and the probability that
  an indicator is true. Input must be daily (time or step). Apply per ensemble
  member; do not average precip first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# indicator

Source-agnostic daily **indicator**: a 0/1 mask from one `--rule` string, then
optional reductions. Apply the rule **per ensemble member** (do not average
`number` first). Plot `--detect first` onset dates (or daily 0/1 /
`--probability` fields) with `plot`. Do not average `number` before mapping
onset dates; use `summarize-dim --dim number --method mean` on `indicator_doy`
when you want a mean onset day-of-year.

`--rule` is taken once. It is a registry name (see
[Named rules](#named-rules-from-the-onset-definition-registry)) or a string of
clauses joined by `and` or `or` (not both).

## When to use

- Windowed thresholds on daily precip (or another variable): “>25 mm in 8
  days”, “<9 mm in 10 days”.
- Sequential onset (ICPAC, AGRHYMET/CHC) as a registry-named `--rule` or
  written out.
- Ensemble probability that the indicator is true (`--probability`).
- First day the indicator is true (`--detect first`), or whether it happens
  at least once (`--detect any`).

**Daily input.** Native `data_interval` of `1 day`, or inferred 1-day spacing
on `time` / `step`. Otherwise run `aggregate-temporal --period daily` (then
`convert-to-totals` if you need mm totals). Daily `mm day-1` rates and daily
`mm` totals are treated as equivalent. Classic forecasts may stay on `step`;
run `step-to-time` when you need calendar onset dates. Restrict the search
window with `select` first (MAM/OND is not built in).

## `--rule` grammar

```text
[not] <variable> <agg> <window> <op> <threshold> [after <Nd>] [within <Nd>]
  ( and | or  [not] … )*
```

`agg`: `sum` | `mean` | `count-above` | `count-below` | `consecutive-above` |
`consecutive-below`. Count/consecutive aggs take a **daily** threshold before
the window (`precip count-below 1 10d >= 7`). Consecutive clauses have no
`<op> <threshold>` — they are true when every day in the window matches.
`after Nd` shifts the clause N days later. `within Nd` is true if the clause
is true on **any** of the next N days (incomplete look-ahead is NaN). `not`
inverts that clause. `after` and `within` cannot appear on the same clause.

### Named rules from the onset-definition registry

Named rules are not hard-coded. They are read from
`references/onset_definitions.toml`, a copy of the repository's onset-definition
registry that states each definition once, with its status and source. Any id in
that file that the grammar can express **exactly** is accepted, for example
`agrhymet-sos-rolling`. An unknown name fails and lists the known ones.

**Refusal.** A registered definition that the grammar cannot express exactly is
refused by name, and the error lists what is missing: per-cell climatological
thresholds, search start dates or windows, calendar dekads, `>=` wet-day tests,
and veto windows counted from the end of the trigger. Today that refuses
`moron-robertson-2014` and its variants, `agrhymet-sos` (calendar dekads) and
`icpac-onset-north` (search start). If you accept the difference, write the
approximating clause string yourself. It is recorded as ad-hoc.

**Legacy aliases** keep working:

- `icpac-onset` → registry `icpac-onset`:
  `precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d`. The
  registry's search start (1 Feb / 1 Aug) and 60-day search window cannot be
  expressed, so the alias runs as the rolling-window core rule. Its output is
  stamped `unregistered-variant`, with the dropped parts in the overrides.
  Restrict the season with `select` first.
- `chc-onset` → registry `sheerwater-chc-onset`:
  `precip sum 10d > 25 and precip sum 20d > 20 after 10d`. This is exactly the
  rule the alias always expanded to, and Sheerwater's `chc_onset` event, so
  results are unchanged. The published AGRHYMET / FEWS NET wording is "at least"
  25 mm and 20 mm on calendar dekads (`agrhymet-sos`); `agrhymet-sos-rolling`
  is that definition on rolling windows with `>=`, if you want it by name.

### Provenance

Every output data variable, and the dataset itself, carries:

- `onset_definition_id`: the registry id. `ad-hoc` for a clause string.
- `onset_definition_hash`: the first 12 hex digits of the SHA-256 of the
  definition's scientific fields (`time_basis`, `trigger`, `confirm`, `veto`,
  `search`, as JSON with sorted keys). For an ad-hoc rule it is the hash of the
  rule string.
- `onset_definition_status`: the registry status (`canonical`, `variant` or
  `candidate`). `unregistered-variant` means a registry rule was modified.
  `unregistered` means an ad-hoc rule.
- `onset_definition_overrides`: JSON. `"{}"` when there are none, and
  `{"dropped": [...]}` when a legacy alias ran without part of its definition.

`--variable` only renames the input variable. It is not recorded as a
modification of the definition.

## Reductions

Applied in order. Default: 0/1 `indicator`, all dims kept.

1. `--cumulative` — once True, stay True (“has it happened yet?”).
2. `--detect first|any` — collapse time. `first` writes `indicator_time`
   (NaT if none) and `indicator_doy` on a datetime axis. `any` writes 0/1.
   Mutex with `--cumulative`. `--detect first` cannot combine with
   `--probability` (dates are not 0/1).
3. `--probability` — mean over `number`. Variable `probability` (`units="1"`).
   No-op on the ensemble axis if `number` is absent (still 0/1).

| Want | Flags |
| --- | --- |
| Daily P(window exceeded) | `--probability` |
| Onset date per member | `--detect first` |
| P(onset has occurred by this date) | `--cumulative --probability` |
| P(event happens at least once) | `--detect any --probability` |
| Onset date map | `--detect first`, then `plot` (do not reduce `number` first) |
| Mean onset day-of-year | `--detect first`, then `summarize-dim --dim number --method mean` on `indicator_doy` |

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py \
    -i <daily.zarr> -o <out.zarr> --rule <name-or-clauses> \
    [--variable NAME] [--time-dim DIM] \
    [--detect first|any] [--cumulative] [--probability]
```

### Arguments

- `--input`, `-i` — daily standard dataset Zarr.
- `--output`, `-o` — output Zarr.
- `--rule` — registry name or clause string (required, once).
- `--variable`, `-v` — override the variable named in every clause.
- `--time-dim` — daily axis (default `time` if length > 1, else `step`).
- `--detect` — `first` or `any`.
- `--cumulative` — running OR along the daily axis.
- `--probability` — ensemble fraction.

## Examples

```bash
# >25 mm in 8 days, ensemble probability
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py \
    -i /tmp/daily.zarr -o /tmp/wet8.zarr \
    --rule "precip sum 8d >= 25" --probability

# ICPAC onset date per member
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py \
    -i /tmp/daily.zarr -o /tmp/onset.zarr \
    --rule icpac-onset --detect first

# P(onset has occurred by each date)
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py \
    -i /tmp/daily.zarr -o /tmp/onset_p.zarr \
    --rule icpac-onset --cumulative --probability
```

Same ICPAC mask written out:

```bash
--rule "precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d"
```

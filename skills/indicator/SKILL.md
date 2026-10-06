---
name: indicator
description: >-
  Apply a boolean indicator to a daily or weekly weather-skills standard
  dataset Zarr (windowed precip thresholds, sequential onset rules) and
  optionally reduce to ensemble probability. Use for ICPAC/CHC rainy-season
  onset, ad-hoc rules like >25 mm in 8 days or <9 mm in 10 days, dry or very
  wet weeks (≤10 mm or ≥50 mm in a week), wet/dry spells, and the probability
  that an indicator is true. Input must be daily or weekly (time or step).
  Apply per ensemble member; do not average precip first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# indicator

Source-agnostic **indicator** on daily or weekly data: a 0/1 mask from one `--rule` string, then
optional reductions. Apply the rule **per ensemble member** (do not average
`number` first). Plot `--detect first` onset dates (or daily 0/1 /
`--probability` fields) with `plot`. Do not average `number` before mapping
onset dates; use `summarize-dim --dim number --method mean` on `indicator_doy`
when you want a mean onset day-of-year.

`--rule` is taken once. It is a named alias or a string of clauses joined by
`and` or `or` (not both).

## When to use

- Windowed thresholds on daily precip (or another variable): “>25 mm in 8
  days”, “<9 mm in 10 days”.
- Sequential onset (ICPAC, CHC) as a named `--rule` or written out.
- Ensemble probability that the indicator is true (`--probability`).
- First day the indicator is true (`--detect first`), or whether it happens
  at least once (`--detect any`).

- Weekly thresholds on weekly data (e.g. the KMSA weekly downscale): “dry
  week ≤ 10 mm” is `precip sum 1w <= 10`.

**Daily or weekly input.** The step is the stamped `data_interval`, or the
spacing on `time` / `step`. It must be a whole number of days (`1 day`,
`7 day`) and evenly spaced. Otherwise run `aggregate-temporal --period daily`
or `--period weekly` first. Classic forecasts may stay on `step`; run
`step-to-time` when you need calendar onset dates. Restrict the search window
with `select` first (MAM/OND is not built in).

**Precip rates become per-step totals.** A precip rate (`mm day-1`, `m s-1`,
…) is multiplied by the step before the rule runs, so thresholds are mm per
step and window sums are mm per window. Daily `mm day-1` is unchanged; weekly
`mm day-1` becomes mm per week. Inputs already in `mm` (e.g. after
`convert-to-totals`) are used as-is. Daily rules (`<9 mm in 10d`, `>50 mm in
one day`) need daily data; they cannot be applied to weekly totals.

## `--rule` grammar

```text
[not] <variable> <agg> <window> <op> <threshold> [after <window>] [within <window>]
  ( and | or  [not] … )*
```

`<window>` is `Nd` days or `Nw` weeks (`1w` = `7d`) and must be a whole
number of steps: on weekly data `1w`, `2w` and `14d` work, `10d` is refused.

`agg`: `sum` | `mean` | `count-above` | `count-below` | `consecutive-above` |
`consecutive-below`. Count/consecutive aggs take a **per-step** threshold (per
day on daily data, per week on weekly data) before the window
(`precip count-below 1 10d >= 7`). Consecutive clauses have no
`<op> <threshold>` — they are true when every step in the window matches.
`after 10d` shifts the clause 10 days later. `within 21d` is true if the
clause is true on **any** of the next 21 days (incomplete look-ahead is NaN). `not`
inverts that clause. `after` and `within` cannot appear on the same clause.

### Aliases

- `icpac-onset` — 3-day sum ≥ 20 mm and **no** 7 consecutive days < 1 mm in
  the next 21 days. Expands to
  `precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d`.
- `chc-onset` — 10-day sum > 25 mm and the **next** 20-day sum > 20 mm.
  Expands to `precip sum 10d > 25 and precip sum 20d > 20 after 10d`.

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
    -i <daily.zarr> -o <out.zarr> --rule <alias-or-clauses> \
    [--variable NAME] [--time-dim DIM] \
    [--detect first|any] [--cumulative] [--probability]
```

### Arguments

- `--input`, `-i` — daily or weekly standard dataset Zarr.
- `--output`, `-o` — output Zarr.
- `--rule` — alias or clause string (required, once).
- `--variable`, `-v` — override the variable named in every clause.
- `--time-dim` — daily or weekly axis (default `time` if length > 1, else
  `step`).
- `--detect` — `first` or `any`.
- `--cumulative` — running OR along the daily axis.
- `--probability` — ensemble fraction.

## Examples

```bash
# >25 mm in 8 days, ensemble probability
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py \
    -i /tmp/daily.zarr -o /tmp/wet8.zarr \
    --rule "precip sum 8d >= 25" --probability

# Weekly forecast (e.g. KMSA weekly downscale, mm day-1): P(dry week, ≤ 10 mm)
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py \
    -i /tmp/weekly.zarr -o /tmp/dry_week.zarr \
    --rule "tp sum 1w <= 10" --probability

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

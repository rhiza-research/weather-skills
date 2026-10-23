---
name: indicator
description: "Apply a yes/no weather rule, such as more than 25 mm of rain in 8 days or a rainy-season onset definition (ICPAC, CHC), to each day or week of a daily or weekly Zarr, and optionally reduce it to an onset date or an ensemble probability. Use for onset dates, wet or dry spells, and the chance that a threshold is met."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# indicator

Writes a 0/1 mask from a `--rule`, evaluated on each date of a daily or weekly series, then optional reductions.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py -i IN.zarr -o OUT.zarr --rule RULE [-v NAME] [--time-dim DIM] [--detect first|any] [--cumulative] [--first-within WINDOW] [--probability]
```

`RULE` is an alias or clauses joined by all `and` or all `or`:
```text
[not] VAR AGG [DAILY_THR] WINDOW [OP THR] [after Nd | within Nd]
```
- `AGG`: `sum`, `mean`, `count-above`, `count-below` (number of steps above/below `DAILY_THR`), `consecutive-above`, `consecutive-below` (every step in the window above/below `DAILY_THR`; no `OP THR`).
- `WINDOW`: `Nd` or `Nw`, starting on the labelled date and looking forward. `OP`: `>=`, `<=`, `>`, `<`.
- `after Nd`: evaluate the clause N days later. `within Nd`: true if the clause is true on any of the next N days. `not` inverts the clause.
- Aliases: `icpac-onset` = `precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d`; `chc-onset` = `precip sum 10d > 25 and precip sum 20d > 20 after 10d`.

### Arguments
- `--input`, `-i` — daily or weekly Zarr on `time` or `step`.
- `--output`, `-o` — output Zarr.
- `--rule` — alias or clause string. Required.
- `--variable`, `-v` — use this variable in every clause instead of the one written in the rule.
- `--time-dim` — axis to evaluate along. Default: `time` if it has more than one value, else `step`.
- `--detect` — collapse the axis: `first` (date of the first true day) or `any` (true at least once).
- `--cumulative` — once true, stay true.
- `--first-within WINDOW` — window such as `7d` or `1w`. Marks the first true day of the input and the `WINDOW − 1` dates before it as 1, earlier dates as 0, and later dates as NaN: each date answers "does the first event fall within WINDOW from here?". Combines with none of the other reductions.
- `--probability` — fraction of ensemble members (`number`) where true; unchanged if there is no `number`.

### Output
- Default: `indicator` (0/1/NaN, `units: 1`) on the input dims.
- `--detect first`: `indicator_time` (first true date or step) and, on a datetime axis, `indicator_doy` (day of year).
- `--detect any`: `indicator` without the time axis.
- `--probability`: `probability` (0–1, `units: 1`).
- `--first-within`: `indicator` is a buffer of 1s ending on the first true day. With `--first-within 3d` and the first true day on day 6: days 1–3 are 0, days 4–6 are 1, day 7 onward is NaN. A gap before the first true day makes every date from the gap on NaN, since the event could be hidden in it. `aggregation_period` is the window.
- Each variable has `long_name` and `indicator_spec` (parsed rule as JSON); the store has `indicator_rule` and `indicator_expanded`.

### Notes
- Thresholds are in mm per step for precipitation (rates are multiplied by the step, so weekly data is mm per week; amounts in mm are used as stored) and in °C for air temperature. Other variables are compared as stored.
- Evaluate on individual members, not an ensemble mean. Windows must be whole steps (`1w`, not `10d`, on weekly data).
- A missing value makes every window that covers it NaN. `--detect first` gives no date when the rule is never true or when a gap comes before the first true day. `--detect any` is NaN when a gap could hide the event. `--probability` is NaN where any member is NaN.
- The last dates of the input cannot be decided (27 days for `icpac-onset`). stderr prints the last decidable date; NaN after it means "not yet known".
- `--detect` cannot be combined with `--cumulative`, and `--detect first` cannot be combined with `--probability`.
- `--first-within` counts from the first date of the input, so start the input at the season start: an event before it is not seen.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/indicator.py -i ecmwf_s2s_kenya_daily.zarr -o onset_p.zarr --rule icpac-onset --cumulative --probability
```
Writes, for each lead day of the 2026-10-12 forecast over Kenya, the fraction of members whose ICPAC onset has happened by then.

## Details

| Want | Flags |
| --- | --- |
| Daily P(window exceeded) | `--probability` |
| Onset date per member | `--detect first` |
| P(onset has happened by each date) | `--cumulative --probability` |
| P(event happens at least once) | `--detect any --probability` |
| Observed onset within the week from each date, post-onset dates left out | `--first-within 7d` |

`indicator_doy` wraps at 1 January, so a mean across members whose onsets straddle the year end is wrong. `and` / `or` stay decided when one side alone decides them (a false side makes `and` false even if the other is NaN). For calendar onset dates from a forecast, convert `step` to valid `time` first.

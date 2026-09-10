---
name: plot-onset
description: Render a rainy-season onset map PNG showing ensemble-mean onset date AND per-cell member agreement in one figure -- cells where few members found an onset are faded, and the percentage of members is drawn over the map (per-cell text on a coarse grid, contour lines on a fine one). Use this for any onset map instead of plot, since a plain mean onset map silently hides how many members it rests on. Takes onset-date's output directly; it derives the day-of-year, mean, and member coverage itself.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_onset.py *)
metadata:
  catalog-group: figure
---

# plot-onset

Onset map carrying both halves of an ensemble onset forecast in one figure:
the ensemble-mean onset date as a discrete date colormap, and how many
members actually found an onset at all.

It takes `onset-date`'s output directly and derives everything it needs
internally — day-of-year conversion, the skipna mean, and the member
coverage percentage. You do **not** chain `day-of-year`, `summarize-dim`,
and `exceedance-probability` in front of it.

## Why this exists instead of `plot`

`onset-date` returns `NaT` for a member that never satisfies the rule, and
an ensemble mean skips those. A plain mean-onset map therefore renders a
cell where 2 of 51 members triggered exactly like one where 49 of 51 agreed
— confident-looking, and badly wrong. This skill makes that visible in the
same figure rather than in a caveat someone has to remember to read:

- Cells below `--low-confidence-pct` (default 10%) are drawn at half alpha.
- The member percentage is annotated per cell on a coarse grid (≤ 200 cells,
  e.g. S2S), or drawn as labeled contour lines on a finer one (e.g. GEFS at
  ~0.25°), where per-cell text would be illegible.
- Cells where no member found an onset are `NaN` and render transparent.

`plot` also cannot render an onset field at all — it computes its colorbar
range with `float(da.max())`, which raises on a date dtype.

## When to use

- Any map of onset results, ensemble or deterministic. A dataset with no
  member dim plots its single onset field with no overlay.
- Pair with the `--start-date`/`--end-date` scale below when comparing onset
  across sources (S2S vs GEFS vs downscaled) for the same init.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_onset.py \
    --input <onset.zarr> --output <onset.png> \
    [--variable VAR] \
    [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] \
    [--low-confidence-pct PCT] [--title TITLE]
```

### Arguments

- `--input`, `-i` — input Zarr: `onset-date` output carrying lat/lon, and
  optionally a member dim (`number`/`member`/`realization`).
- `--output`, `-o` — output PNG.
- `--variable`, `-v` — the onset variable to map; must be `datetime64`.
  Default: the input's only `datetime64` data variable (errors if there are
  several, naming them).
- `--start-date` / `--end-date` — the dates the **color scale** starts and
  ends at. These set the scale only; they do **not** subset the data, and
  onsets outside the range are drawn in the end colors. Set them to the
  forecast's first day and the last day that still left a full onset search
  window (i.e. `n_steps - search_days`), which is what makes two sources
  comparable. Omit them and the scale is derived from the data's own range
  instead — fine for a one-off look, not comparable across forecasts, and
  the skill prints a note saying so.
- `--low-confidence-pct` — fade cells where fewer than this percentage of
  members found an onset. Default `10`.
- `--title` — optional title. Default: `Onset date — <variable>`.

### Absolute dates required

The onset variable must be `datetime64`, not `timedelta64`. A `timedelta64`
onset is elapsed lead time with no calendar date to place on a date scale;
run `step-to-time` on the forecast *before* `onset-date` so the onset comes
out absolute. The skill rejects a duration variable with that pointer rather
than plotting it against an arbitrary origin.

### Year-crossing forecast windows

Internally the map works in *days since a reference date* (`--start-date`,
or the earliest onset in the data), not raw day-of-year. A forecast window
that crosses New Year — a Nov/Dec init over southern Africa, say — wraps
day-of-year from 365 back to 1, which would average to nonsense and invert
the color scale. Days-since-reference is identical to day-of-year within a
year and stays correct across the boundary, and the colorbar still reads as
real dates.

The one case this cannot fix is a genuinely bimodal onset (members splitting
between an early and a much later onset): their *mean* is a date in between,
which no amount of scaling makes meaningful. The member-coverage overlay
does not detect that — it reports how many members found an onset, not how
much they agree on when.

### Output

One PNG. The colorbar is a discrete five-band scale (sand → green → cyan →
pink-purple → gray, four shades each) labeled with real dates at the band
edges. Remaining dims must reduce to lat/lon (plus the member dim); anything
else — a leftover `step`, say — is an error pointing at `select`.

### Provenance

The output stamps its `weather_skills_history` chain into the PNG's binary
`tEXt` chunks, and (when the chain validates) a circular
`weather-skills provenance verified` mark on the pixels, bottom-right. Read
the chain with the `provenance` skill — `Read` cannot open it.

## Examples

```bash
# Ensemble onset map with a scale fixed to the forecast window.
# 46 steps with the default 21-day search window -> last searchable day is
# init + (46 - 21) days.
uv run ${CLAUDE_SKILL_DIR}/../onset-date/scripts/onset_date.py \
    -i /tmp/s2s_daily.zarr -o /tmp/onset.zarr --definition ICPAC --variable tp
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_onset.py \
    -i /tmp/onset.zarr -o /tmp/onset.png \
    --start-date 2026-11-01 --end-date 2026-11-26 \
    --title "ICPAC onset — Kenya (S2S)"
```

```bash
# Same scale, second source -> the two maps are directly comparable.
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_onset.py \
    -i /tmp/onset_gefs.zarr -o /tmp/onset_gefs.png \
    --start-date 2026-11-01 --end-date 2026-11-26 \
    --title "ICPAC onset — Kenya (GEFS)"
```

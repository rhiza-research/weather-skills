---
name: convert-to-totals
description: Convert precipitation rates to period totals (mm) by multiplying by the stamped aggregation_period — calendar months by their real length. Use as a terminal step before plotting, after aggregate-temporal. Converts precipitation only; other variables pass through unchanged. Refuses precip totals (would double-count) and overlapping intervals — run select on time/step first. Default --min-coverage 1.0 drops incomplete intervals (listed on stderr); lowering it fills missing days with the bin mean and is rarely appropriate.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# convert-to-totals

Terminal conversion: **precipitation rate × stamped `aggregation_period` →
amount (`mm`)**.

Intended only before drawing or export. Converts precipitation-rate variables only
(recognized by CF `standard_name`, or a precip name such as `precip`, `tp`,
`pr`, or `total_precipitation` with rate units). Other variables
(temperature, pressure, wind, …) pass through unchanged. Only a mean rate
converts: a weekly `max`/`min` (`cell_methods` maximum/minimum) is refused,
since the wettest day × 7 is not a weekly total. Refuses precip totals
(`cell_methods` with `sum`, or amount units) — multiplying an amount by the
period would double-count.

## When to use

- After `aggregate-temporal` (which stamps `aggregation_period`,
  `aggregation_coverage`, and `cell_methods` with `mean`/`minimum`/`maximum`).
- When you need precipitation depth totals (`mm`) for display, not further
  rate math.
- Native-only cubes (`data_interval` or CF bounds, but no `aggregation_period`) will not
  convert — aggregate first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py \
    -i <rates.zarr> -o <totals.zarr> \
    [--variable NAME ...] [--min-coverage 1.0] [--time-dim DIM]
```

### Arguments

- `--min-coverage` — drop intervals whose `aggregation_coverage` is below this
  (0–1). Default **1.0** (every native sample has finite data).
  `aggregate-temporal` keeps incomplete bins and only stamps coverage; this
  flag is what filters them. All-NaN unpublished forecast leads stamp
  coverage 0 and are dropped at the default. **Keep the default.** Lower it
  only in the rare case a task explicitly asks for totals from incomplete
  bins: an incomplete bin's mean rate is multiplied by the full period, so the
  missing days are filled with that mean — an estimate, not an observed total.
  When that happens the skill warns on stderr and keeps `aggregation_coverage`
  on the output so the estimated bins stay identifiable; say so when reporting
  those totals. Dropped intervals are listed on stderr with their coverage.
  If no intervals remain, the skill errors and says how to recover: usually
  the data end before the last period does, so fetch a window that ends on or
  before the fetcher's `--probe-latest` day (`resolve-time <query> --as-of
  <that day>`).
- `--variable`, `-v` — limit to named precipitation variables (default: every
  precipitation-rate variable). Naming a variable that is not precipitation
  exits non-zero; with no `--variable` and no precipitation rate in the input,
  the skill exits non-zero.
- `--time-dim` — time/step dim for the overlap gate (default: CF time or `step`).

There is no `--aggregation-period` override. The period comes from the
stamped attr.

### Period length

Each time label starts an interval of `aggregation_period`. Fixed periods
(`1 day`, `7 day`, `1 dekad`) multiply every label by the same length.
Calendar periods (`1 month`, `3 month`, `1 year`) multiply each label by its
real length — January 31 days, February 28 or 29 — so a 2 mm/day January is
62 mm, not 2 × an average month. Calendar periods need month-start time labels:
convert before `select` collapses the time axis.

### Gate

Every interval must end at or before the next label: consecutive
`aggregation_period` intervals may touch but not overlap. End-over-end weekly
means (Δt = 7 day, period = 7 day) are allowed; a monthly series passes with
each month's own length. Overlapping series (rolling `--window`, two 21-day
intervals 10 days apart, or a single extra label one day after another) are
refused — **run `select`** (`--dim time` or `--dim step`, by `--index` or
`--value`) to keep a non-overlapping subset, then convert-to-totals. A
**single** time/step point (one aggregated bin) is allowed — including after
`select` has already collapsed that dim away (spatial-only map) for a fixed
period. In that case conversion uses the stamped `aggregation_period` alone, and
`--min-coverage` checks the scalar `aggregation_coverage` that `select` keeps;
the overlap gate does not apply.

### Output metadata

- `units` always `mm`. A mass-flux rate (`kg m-2 s-1`, as in raw CMIP6 `pr`)
  converts through liquid-water density (1 kg m-2 = 1 mm); a rate that cannot
  become a depth exits non-zero.
- Precip amount `standard_name`.
- Precip `long_name` → `Total precipitation` (replaces product/rate names; the
  quantity is now a period total). Rate-like `GRIB_name` is rewritten the same
  way, and the product's rate `comment` / `short_name` are dropped.
- `cell_methods` → `{dim}: sum`.
- `aggregation_coverage` removed when every converted bin is complete; kept
  when `--min-coverage` below 1.0 let incomplete (estimated) bins through.
- `aggregation_period` **kept** so plotters can crop the nested precip
  color scale (daily / week / month / season windows).

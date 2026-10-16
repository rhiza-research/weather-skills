---
name: difference
description: Subtract one weather-skills standard dataset Zarr from another (A − B) with xarray inner-join alignment and broadcasting — e.g. anomalies as a field minus its baseline mean, or a scenario-minus-historical change map. Use whenever two datasets must be compared cell-by-cell as a difference field.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# difference

Source-agnostic subtraction of two datasets: the first input is the minuend
(A), the second the subtrahend (B). Subtraction is xarray-aligned — inner
join on shared dims, broadcasting over dims present on only one side — so a
`(time, latitude, longitude)` field minus a `(latitude, longitude)` baseline
(e.g. a time-mean from `summarize-dim`) yields per-time anomalies.

## When to use

- Anomaly vs climatology: a field minus its baseline mean (e.g. SST
  anomalies as `sst.zarr` minus a `summarize-dim --dim time --method mean`
  baseline, or CHIRPS minus `clim-fetch`). When plotting rainfall
  anomalies, omit `theme.colormap` so the default diverging millimetre classes
  apply.
- Scenario minus historical: a change map (e.g. a CMIP6 SSP time-mean minus
  the historical time-mean = projected change by 2050).
- Any cell-by-cell difference of two datasets on a shared grid (forecast
  minus observations, model A minus model B). If the lat/lon coordinates
  differ, `coarsen` or `downscale` first so the inner join hits the same
  points. That alignment is for this subtraction; drawing the two
  side by side does not need it.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i a.zarr -i b.zarr --output <out.zarr> \
    [--variable VAR ...]
```

Write to a new path: `--output` is replaced, so pointing it at an input
destroys that input.

### Arguments
- `--input`, `-i` — input Zarr; pass exactly twice. The first is A (the
  minuend), the second is B (the subtrahend); any other count exits non-zero.
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — repeatable; restricts the difference to the named data
  variable(s), each in BOTH inputs (otherwise exits non-zero listing each
  input's variables). Default (unset) differences every data variable the two
  inputs share; inputs sharing none exit non-zero (`edit-coords --rename` one first). Data
  variables not differenced are dropped from the output.

### Alignment

Shared dims are aligned with an inner join, so only overlapping coordinate
labels are kept; dims present on only one input broadcast. Inputs' lat/lon are
rounded to 5 decimals on open, so float noise (`5.9500000001` vs `5.95`)
aligns, but grids offset from each other (CHIRPS 0.05° centres vs a 0.1°
grid) share few or no labels — put both on one product's exact grid with
`coarsen --reference-grid` (or `downscale --reference-grid` if the target is
finer) first. If no labels overlap along a dim, the skill exits non-zero
rather than writing an empty result.

A shared dim with no index coordinate is paired by position (element *i* of A
minus element *i* of B); differing sizes exit non-zero.

Broadcasting is one-sided: if A and B each have a dim the other lacks (a
`time` series minus a `dayofyear` climatology, a `step` forecast minus `time`
observations) the skill exits non-zero instead of pairing every label of one
with every label of the other. Put both on the same axis first (`step-to-time`;
`clim-fetch` expands a climatology onto the field's `time`).

### Operand dtypes

Boolean and unsigned-integer variables are cast before subtracting: boolean
becomes a signed integer (bool subtraction is nonsensical), and an unsigned
integer is promoted to a signed integer wide enough to hold negatives, so a
result that should be negative is not wrapped around modulo `2**nbits`. Signed
integer, floating-point, and other dtypes are left untouched.

### Input units

Values are subtracted with their units: B is converted to A's units first
(`mm hr-1` from `mm day-1` works; °C minus K works), and the output keeps A's
attrs. Incompatible units (a rate minus an amount, `mm day-1` minus `mm`) exit
non-zero. A temperature difference (including K minus K) is written in
`delta_degC`, the unit of a temperature *difference* (2 delta_degC = 2 K), so
converting it never adds the 273.15 offset of an absolute temperature.
Precipitation totals (amount units or `cell_methods` sum) whose
`aggregation_period` differs (a 7-day total minus a 30-day total) exit
non-zero; aggregate both to one `--period` first.

### Output

One data variable per differenced variable, holding A − B on the aligned
(and broadcast) dims. Each variable carries A's attrs only (B's
`aggregation_period`, `cell_methods`, etc. are dropped); dataset attrs come
from the first input.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

SST anomalies are a two-skill recipe: first the `summarize-dim` skill produces a
time-mean baseline (e.g. `/tmp/sst_baseline.zarr` from `/tmp/sst.zarr`), then
this skill subtracts that baseline from the field, broadcasting over time. Only
the `difference` step is shown here; run `summarize-dim` separately to make
the baseline.

```bash
# Field minus its time-mean baseline (the baseline produced by summarize-dim),
# broadcast over time.
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/sst.zarr -i /tmp/sst_baseline.zarr \
    --output /tmp/sst_anom.zarr
```

```bash
# Projected change: scenario time-mean minus historical time-mean.
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/ssp245_mean.zarr -i /tmp/historical_mean.zarr \
    --output /tmp/change_2050.zarr
```

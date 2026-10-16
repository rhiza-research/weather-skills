---
name: summarize-dim
description: Summarize one or more named dimensions of a weather-skills standard dataset Zarr with a statistic (mean, std, min, max, sum, median) — e.g. ensemble spread as the std across `number`, model disagreement as the std across a model dim, or a time-mean baseline for anomalies. Use whenever a dataset needs a statistical summary along a named dimension (the dim disappears). For coarser time windows that keep a time axis, use aggregate-temporal.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# summarize-dim

Source-agnostic statistical summary along named dims. Summarizes the
requested dims of each selected data variable with one statistic; data
variables that carry none of the requested dims pass through untouched.
NaNs are skipped (xarray's default `skipna`). The summarized dim
**disappears** from the output — unlike `aggregate-temporal`, which keeps a
coarser time/step axis.

## When to use

- Ensemble spread — "where is the forecast most/least certain":
  `--dim number --method std` on an ensemble forecast yields the spread
  field (low std = high certainty).
- Model disagreement: `--method std` across a model dim on a multi-model
  dataset (e.g. one built with `concat` along a new dim).
- Building a baseline for `difference`: `--dim time --method mean` yields a
  climatological-mean field to subtract from a time-resolved field for
  anomalies.
- Any other named-dim statistic: ensemble mean, spatial max, member median.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py --input <in.zarr> --output <out.zarr> \
    --dim DIM [--dim DIM ...] --method mean|std|min|max|sum|median \
    [--variable VAR ...] [--lat-weighted]
```

Write to a new path: `--output` is replaced, so pointing it at the input
destroys the input.

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--dim` — dimension to summarize. Repeat once per dimension to summarize
  several in one run. Each name must be a dim of the input.
- `--method` — statistic applied along the summarized dimension(s): `mean`,
  `std`, `min`, `max`, `sum`, or `median`.
- `--variable`, `-v` — repeatable; restricts the summary to the named data
  variable(s), each a data variable of the input. Default (unset) is every data
  variable. Each selected variable is summarized over the requested dims it
  carries; unselected variables, and selected ones carrying none of the dims,
  pass through unchanged.
- `--lat-weighted` — with `--method mean`, apply cosine-latitude weights when
  summarizing the latitude dim (must include that dim in `--dim`).

### Method and units

Every method keeps each variable's attrs (`keep_attrs`), so the output
variable carries the input's `units` unchanged. For `mean`, `std`, `min`,
`max`, and `median` that is physically correct — the statistic is in the same
units as the values. `sum` along a dim is the one case where the units
semantics can change (N summed values in `mm` are a total, not another `mm`
sample in the same sense); this skill performs no unit math or relabeling
either way — use `unit-convert` to restamp units when needed. (For temporal
totals specifically, use `aggregate-temporal` then `convert-to-totals`.)

The statistic is appended to CF `cell_methods` (`number: standard_deviation`,
`latitude: longitude: mean`). `std` and `sum` also rewrite `long_name`
(`Standard deviation of Total precipitation across number`) and drop the
product's `GRIB_name` / `short_name` / `comment`, so a plot does not label an
ensemble spread as the quantity itself. `standard_name` stays, as CF encodes
a statistic of a quantity with its `standard_name` plus `cell_methods`.

NaNs are skipped (xarray's default `skipna`). Two method-specific
conventions follow from that:

- `sum` uses `min_count=1`: a slice that is entirely missing yields `NaN`,
  not `0`, so "no data" is not silently reported as a real zero total.
- `std` uses `ddof=1` (the sample standard deviation), matching the
  ensemble-spread convention that spread across members is a sample estimate
  rather than a population sigma. A `std` over a size-1 dim is therefore NaN
  (a single sample has no sample spread).

### Output

The selected data variables with the requested dims summarized. A summarized
dim disappears from the output (along with its coordinates) once no data
variable carries it; a dim still carried by a pass-through variable stays.
Remaining dims, coords, and pass-through variables are unchanged.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# Ensemble spread: where is the forecast most/least certain.
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_spread.zarr \
    --dim number --method std
```

```bash
# Time-mean baseline to feed `difference` for anomalies.
uv run ${CLAUDE_SKILL_DIR}/scripts/summarize_dim.py -i /tmp/sst.zarr -o /tmp/sst_baseline.zarr \
    --dim time --method mean
```

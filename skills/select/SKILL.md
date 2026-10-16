---
name: select
description: Select entries along one named dimension of a weather-skills standard dataset Zarr, by integer position or by coordinate value. A single selection collapses the dimension and drops the coordinates it leaves scalar, so outputs from different sources are ready to concat — e.g. pick the same forecast week from several model datasets before merging them along a new model dim.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# select

Entry-selection primitive. Picks entries along one named dimension of an
standard dataset, either by integer position (`--index`) or by coordinate value
(`--value`), and writes a new standard dataset. A single selection collapses the
dimension and also drops every coordinate variable it leaves scalar; multiple
selections keep the dimension with just those entries, in the order given.
Everything else — untouched dims, coords, data variables, and attrs — passes
through unchanged. Works on gridded and point_obs datasets alike.

## When to use

- To align inputs for `concat`: select the same entry (e.g. week-1, `--dim
  step --index 0`) from each of several forecast datasets, then concatenate
  the selected outputs along a new `model` dim. The collapse-and-drop
  semantics make the outputs merge cleanly — a leftover scalar coord on the
  selected dim would otherwise block or pollute the merge.
- To pull a single ensemble member, forecast step, timestamp, or station out
  of a larger standard dataset.
- To subset a dimension to a handful of entries in a chosen order.
- Before `convert-to-totals`, when the time/step axis still overlaps
  (rolling `--window`, or more intervals than you want as independent
  totals). Pick a non-overlapping subset (`--dim time` or `--dim step`,
  `--index` or `--value`); convert-to-totals refuses overlapping labels
  rather than silently thinning the axis.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py --input <in.zarr> --output <out.zarr> \
    --dim DIM (--index N [--index N ...] | --value V [--value V ...])
```

### Arguments

- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr, replaced if it exists. Use a new path;
  pointing it at the input destroys the input.
- `--dim` — the dimension to select along (exactly one per run).
- `--index` — integer position to select; repeat once per position. Plain
  integers only; negative positions count from the end (`-1` is the last
  entry). Mutually exclusive with `--value`.
- `--value` — coordinate value to select; repeat once per value. Mutually
  exclusive with `--index`.

### Selector semantics

`--index` positions addressing the same element are an error — including a
negative alias of an already-given position (on a size-4 dim, `--index 0
--index -4` name the same element). An out-of-range position errors naming the
position, the dim, and its size.

`--value` parses each literal against the dim coord's dtype:

| Coord dtype | Literal form | Example |
| --- | --- | --- |
| `datetime64` | naive ISO datetime string (no timezone suffix, no `now`/`today`) | `2026-06-01`, `2026-06-01T06:00` |
| cftime (model calendars: `noleap`, `360_day`, ...) | `YYYY-MM-DD[THH[:MM[:SS]]]`, read in the coord's calendar | `2026-02-30` on `360_day` |
| `timedelta64` | pandas-style timedelta string | `7D`, `168h` |
| numeric | int or float literal (no underscores, `nan`, or `inf`) | `0`, `1.5` |
| string | verbatim, including NumPy `StringDType` (Zarr v3 station ids) | `TA00001` |

Matching is exact, with no nearest-neighbour lookup. The one exception is float
coords, which match within 1e-9, so `--value 0.3` finds a label stored as
`0.30000000000000004`. A value that isn't on the coord errors with the first
few available values. A value that matches duplicate labels errors and points
to `--index`, as does `--value` on a dim with no coordinate variable. A literal
that can't be parsed for the coord's type errors with the expected form.

### Output

A single selection (one `--index` or one `--value`) collapses the dim — it
disappears from the output — AND drops leftover scalar coordinates on that dim
(e.g. `step`, `valid_time`). Point identity is kept as scalar coords:
`station_id` / `point_id`, `name`, `latitude` / `longitude`, `country`. The
selected bin's `aggregation_coverage` is also kept as a scalar coord, so
`convert-to-totals` still refuses a partial interval after the time/step axis
is gone. Collapsing a time axis stamped with a calendar `aggregation_period`
(`1 month`) rewrites it to that bin's real length (`28 day` for February).
Other coordinates that were already scalar on the input pass through. Multiple selections keep the dim with
exactly those entries, in the order the flags were given (args order = output
order; `--index 2 --index 0` reverses those two entries). Selecting by
`--value` produces the same data as selecting the corresponding positions by
`--index`.

All other dims, coords, data variables, values, and `weather_skills_*` attrs
pass through unchanged. Any argument, input, or selection error exits with
code 2, prints an `Error:` message, and writes no output. That covers a
missing input, an unknown `--dim`, an out-of-range `--index`, a bad or absent
`--value`, and an output path that is an existing plain file.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

Pick week-1 from two weekly forecast datasets, then merge the selected
outputs along a new `model` dim with the `concat` skill
(`--dim model --coords ECMWF,GFS`):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py -i /tmp/ecmwf_weekly.zarr -o /tmp/ecmwf_w1.zarr \
    --dim step --index 0
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py -i /tmp/gfs_weekly.zarr -o /tmp/gfs_w1.zarr \
    --dim step --index 0
```

Each output has no `step` dim and no `step` coord, so the two stores differ
only along the new `model` axis when concatenated.

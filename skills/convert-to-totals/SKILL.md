---
name: convert-to-totals
description: "Convert precipitation mean rates in a time-aggregated weather-skills Zarr into period totals in mm, by multiplying each interval's rate by its stamped aggregation period (calendar months by their real length). Use when you need weekly, monthly, or seasonal rainfall amounts for a map, table, or comparison."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# convert-to-totals

Turns precipitation rates (e.g. `mm day-1`) into amounts (`mm`) for each aggregated interval.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py -i RATES.zarr -o TOTALS.zarr [-v VAR ...] [--min-coverage 1.0] [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — input Zarr of precipitation rates stamped with `aggregation_period`.
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — precipitation variable to convert; repeat for several. Default: every precipitation-rate variable. Naming a non-precipitation variable exits non-zero.
- `--min-coverage` — drop intervals whose `aggregation_coverage` (share of samples with data, 0–1) is below this. Default: `1.0`.
- `--time-dim` — interval axis. Default: `time`, or `step` when there is no `time` axis or `time` has one value.

### Output
- Each converted variable is rate × interval length, in `mm`, with the precipitation-amount `standard_name`, `long_name` `Total precipitation`, and `cell_methods` `{dim}: sum`. Product rate attrs (`comment`, `short_name`, `step_type`) are dropped. `aggregation_period` is kept.
- Fixed periods (`1 day`, `7 day`, `1 dekad`) use one length for every label; calendar periods (`1 month`, `1 year`) use each label's real length (January 31 days, February 28 or 29).
- Mass-flux rates (`kg m-2 s-1`) convert through water density (1 kg m-2 = 1 mm).
- Other variables pass through unchanged. Dropped intervals are listed on stderr. `aggregation_coverage` is removed when every remaining interval is complete.

### Notes
- Input must be a precipitation **mean** rate with `aggregation_period` on the variable (written by aggregate-temporal). Missing `aggregation_period`, a `max`/`min` (`cell_methods` maximum/minimum), or an amount (`mm` units or `cell_methods` sum) exits non-zero.
- Intervals must not overlap: each label's interval must end at or before the next label. Back-to-back windows pass; a rolling series is refused, so keep a non-overlapping subset of times first. A single interval, or a dataset whose time axis was already collapsed to one value, always passes.
- Keep `--min-coverage 1.0`. A lower value multiplies an incomplete interval's mean by the full period, filling missing days with that mean; the skill warns and keeps `aggregation_coverage` to mark those estimates.
- When no interval meets `--min-coverage`, it exits non-zero; usually the data end before the last period does, so end the input on the last available day.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py -i chirps_kenya_weekly.zarr -o chirps_kenya_weekly_mm.zarr
```
Writes weekly CHIRPS rainfall totals in mm over Kenya, dropping any week with missing days.

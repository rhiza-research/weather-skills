---
name: step-to-time
description: "Relabel a single-init forecast Zarr by valid date instead of lead time: the step (lead) axis becomes a time axis holding init + step. Use when a forecast must line up with observations or other date-labeled data for plotting, differencing, or verification."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# step-to-time

Replaces a forecast's `step` (lead time) axis with a `time` axis of valid times, `init + step`.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py -i IN.zarr -o OUT.zarr
```

### Arguments
- `--input`, `-i` — forecast Zarr with a `step` axis (lead times) and a scalar `time` coord holding the init.
- `--output`, `-o` — output Zarr.

### Output
- Same variables, values, and other dims (`number`, latitude, longitude); `step` is replaced by a `time` axis of the same length (CF `standard_name: time`, `axis: T`).
- Valid times are `datetime64[ns]`, or `cftime` objects for a model calendar (`noleap`, `360_day`). They keep the init's time of day.
- `step_bounds` become `time_bounds`. The `step` and scalar `time` coords and any `valid_time` coord are dropped; the init is kept in the dataset attr `weather_skills_forecast_init` (ISO 8601).
- Forecast steps are labeled at the start of each interval, so `step = 0` of daily precipitation becomes the init date.

### Notes
- The input must hold one init. A `time` axis (already on dates, or a multi-init cube with both `time` and `step`) exits 2; select one init first.
- Also exits 2 on a missing (NaT) init or a `step` axis that is not strictly increasing.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py -i ecmwf_s2s_kenya.zarr -o ecmwf_s2s_kenya_valid.zarr
```
Writes the 2026-10-12 ECMWF forecast over Kenya with a `time` axis running 2026-10-12, 2026-10-13, … instead of leads 0, 1, … days.

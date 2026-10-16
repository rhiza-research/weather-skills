---
name: step-to-time
description: Realize a forecast dataset's `step` lead-time axis as wall-clock valid times (`time = init + step`), replacing the `step` dim with a `time` dim. Use it to compare a forecast against observations — e.g. before difference, verify, or a time-series figure against a time-based dataset.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# step-to-time

Axis-realization primitive. A forecast dataset labels its temporal axis with
lead times — a `step` dim (`timedelta64`) plus a scalar `time` coord holding
the forecast init date — while observation datasets carry a wall-clock `time`
dim (`datetime64`). Skills that compare the two need both inputs on the same
kind of axis. This skill computes `valid_time = init + step` and rewrites the
standard dataset with the `step` dim replaced by a `time` dim labeled with those valid
times. After fetch, interval fields are left-labeled, so `step = 0` becomes
the init calendar date (the first 24h of rain for daily precip). `step_bounds`
(written for uneven leads) become `time_bounds`.

## When to use

- To compare a forecast against observations: run it on the forecast before
  feeding both inputs to `difference`, `verify`, or a time-series figure
  against a time-based dataset (e.g. CHIRPS, IMERG, station data).
- Whenever a downstream consumer needs the forecast's values labeled by the
  date they are valid for rather than by lead time.

## Input precondition

The input must have a `step` dim whose values are `timedelta64` lead times AND
a scalar (0-d) `time` coord holding the forecast init date. Spatial dims
(`lat`/`lon`) are optional — a cube already reduced over space (e.g.
`dims=['step', 'number']`) is accepted. The init may be a standard
`datetime64` or, for a non-standard model calendar (`noleap`, `360_day`), an
object-dtype `cftime` datetime. An input that already has a `time` dim is
already on a wall-clock axis and is rejected; an input that has BOTH a `time`
dim and a `step` dim (a multi-init/hindcast cube) is rejected; `select` a
single init first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py --input <in.zarr> --output <out.zarr>
```

### Arguments
- `--input`, `-i` — input Zarr containing a forecast dataset with a `step` dim
  and a scalar `time` init coord.
- `--output`, `-o` — output Zarr.

### Output

Same data variables, values, and non-temporal dims/coords (`number`, lat/lon)
as the input, EXCEPT the `step` dim is replaced by a `time` dim of the same
length whose coord holds the realized valid times (`init + step`), stamped with
CF attrs (`standard_name: time`, `axis: T`). For a `datetime64` init the
realized axis is cast to `datetime64[ns]` so the output resolution is consistent
regardless of the init/step source resolution; for a `cftime` init the realized
axis stays as `cftime` objects. The valid times carry the init's time-of-day
(midnight if the init is date-only, e.g. `datetime64[D]`). The scalar `time`
init coord and the `step` coord are dropped, as is any pre-existing
`valid_time` coord (it would otherwise pass through stale alongside the new
axis); the init date is written to the dataset attr `weather_skills_forecast_init`
(ISO 8601, the `cftime` ISO string for a non-standard-calendar init).

The skill exits 2 with a message when the input has no `step` dim or no
scalar `time` init coord (core's input check reports this as a missing
`init_time`), already has a `time` dim — including a multi-init cube with
both `time` and `step`, which needs `select` on one init first — has a
missing (NaT) init, or has `step` values that are not strictly increasing.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py -i /tmp/ecmwf_daily.zarr -o /tmp/ecmwf_valid.zarr
```

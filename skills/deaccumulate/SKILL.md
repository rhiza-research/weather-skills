---
name: deaccumulate
description: Convert a leftover cumulative-since-init forecast variable along its `step` axis into per-step rates (precip → mm day-1). Fetchers (`ecmwf-fetch`, `dynamical-fetch`, `kenya-forecast-fetch`, `cumulus-fetch`, `neuralgcm-fetch`) already write rates — do not run this after them. Use on older cumulative archives that still have amount units.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# deaccumulate

Source-agnostic deaccumulation primitive. Some forecast variables are stored as
values accumulated from the forecast init time. This skill turns one of them
into per-step increments `arr[i+1] - arr[i]` along `step`, clipped at zero.

## When to use

- A forecast cube that is still cumulative since init (amount units such as
  `mm`, `m`, `kg m-2`, growing along `step`) — typically a leftover archive,
  not a current fetcher output.
- Other non-negative cumulative fields that grow along `step` (e.g. downward
  surface radiation `ssrd` / `strd` in `J m-2`).

## When not to use

- **Current fetcher outputs** (`ecmwf-fetch` `tp`, `dynamical-fetch`
  `precipitation_surface`, `kenya-forecast-fetch` precip / precip_downscaled /
  precip_downscaled_daily, `cumulus-fetch` `tp`, `neuralgcm-fetch` `tp`) —
  already rates (`mm day-1`). The skill refuses them. For period amounts, run
  `aggregate-temporal` then `convert-to-totals`.
- CHIRPS, IMERG, station precip, and any variable whose `units` are per-time
  (`mm day-1`, `kg m-2 s-1`) or whose `standard_name` ends in `_rate` / `_flux`.
- Signed or downward accumulations (ECMWF evaporation `e`, net thermal
  radiation `str` / `ttr`, sensible/latent heat fluxes). Clipping would zero
  them, so the skill refuses any variable with negative values.
- Data cumulative along `time` rather than `step` — the input must be a
  forecast with a `step` dim.

## Input checks

All refusals exit 2 with an `Error:` message naming the variable:

- `units` with a per-time denominator (`mm/day`, `kg m-2 s-1`, `W m-2`) or a
  `standard_name` ending in `_rate` / `_flux` — already a rate.
- Negative values (beyond GRIB packing noise) — see above.
- Precip-named variables (`tp`, `precip`, …) without a `units` attr.
- No `step` dim, fewer than two steps, or a non-increasing `step` axis.
- More than one data variable and no `--variable`, or an unknown `--variable`.

A variable with neither `units` nor `standard_name` (and a non-precip name)
cannot be checked: the skill prints a stderr warning and proceeds.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py --input <in.zarr> --output <out.zarr> \
    [--variable NAME]
```

### Arguments
- `--input`, `-i` — input Zarr containing a forecast dataset with a `step` dim.
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — variable to deaccumulate. Required when the input has
  more than one data variable.

### Output

- The `step` axis is one shorter: each cell is the interval
  `[step[i], step[i+1])`, labeled at its **left** edge; the last input step
  is dropped. `valid_time` (if present) is `time + step` of the left edge.
- Precip amounts (`mm`, `m`, `kg m-2`) become `mm day-1`
  (`lwe_precipitation_rate`): each increment is divided by its own interval
  length, so irregular leads (e.g. 0, 7, 10, 14 days) are handled.
- Other variables keep their attrs; values are per-interval increments in the
  input units (not divided by the interval).
- Increments are clipped at zero, so a reset or float noise that makes the
  accumulation drop gives 0 rather than a negative rate. NaN stays NaN; no
  step is filled.
- With no step 0 in the input, the amount accumulated before the first step is
  not recovered: the first cell starts at the first input step.
- Interval stamp: evenly spaced steps get `data_interval` (e.g. `1 day`);
  irregular steps get CF `step_bounds` (start, end per cell) instead.
- Other data variables (with `--variable`) are not differenced; they are cut
  to the same steps and keep their values at each left edge.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Composition with aggregate-temporal

Deaccumulated precip is a **per-step rate** (`mm day-1`). Aggregate it with
`aggregate-temporal --method mean` (or min/max), then run `convert-to-totals`
when you need period amounts. Do not aggregate a still-accumulated variable —
deaccumulate first. Per-step rates can also be compared directly to daily
rate observations (IMERG, CHIRPS, stations).

## Examples

```bash
# Leftover cumulative-since-init cube (current fetchers already write rates)
uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py -i /tmp/cumulative_tp.zarr -o /tmp/tp_rates.zarr \
    --variable tp
```

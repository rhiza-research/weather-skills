---
name: standardize-anomaly
description: Compute a standardized anomaly (z-score) — (ds[var] - clim[var_avg]) / clim[var_std] — for one or more --variable names, given a data Zarr and a climatology Zarr (e.g. from clim-fetch) with matching dims. Dimensionless output, converts the climatology to the field's units and refuses incompatible units. The climatology must cover every input timestamp and cell; run step-to-time on a forecast first. Use for anomaly detection, SPI/SPEI-style standardized indices, or comparing a value's unusualness across variables/locations on the same scale. Not for a plain physical-unit anomaly (field minus mean, no division) — use difference for that.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# standardize-anomaly

Computes a **standardized anomaly** — the z-score of a field against a
climatology's mean and spread — not a plain physical-unit anomaly. The two
are genuinely different quantities:

- **Anomaly**: `field − mean`. Still in the field's original units (a
  temperature anomaly in °C, a precip anomaly in mm/day). Already covered by
  `difference` (subtract a climatology mean from a field).
- **Standardized anomaly / z-score**: `(field − mean) / std`. Dimensionless.
  The basis for indices like SPI/SPEI. What this skill computes.

For each `--variable NAME`, reads `NAME` from `--input` and `NAME_avg` /
`NAME_std` from `--climatology` (e.g. `clim-fetch`'s own output naming),
aligns them on shared dims (see "Alignment"), and writes `NAME_anomaly`.

## When to use

- Anomaly detection: how unusual is today's value relative to this
  day-of-year's typical mean and spread?
- Standardized indices (SPI/SPEI-style): needs the climatology's mean *and*
  std, not mean alone.
- Comparing unusualness across variables or locations on one common,
  dimensionless scale (a z-score of +2 means the same thing for precip in
  Nairobi as for temperature in Lagos; a plain anomaly in mm/day and one in
  °C do not compare at all).

Not for a plain physical-unit anomaly — use `difference` with a mean-only
baseline for that; no division, output keeps the field's original units.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py \
  --input <ds.zarr> --climatology <clim.zarr> --variable NAME [--variable NAME ...] \
  --output <out.zarr>
```

### Arguments

- `--input`, `-i` — the data Zarr (e.g. an observation or forecast field).
- `--climatology` — the climatology Zarr, carrying `NAME_avg`/`NAME_std` for
  every `--variable` requested (e.g. `clim-fetch`'s output).
- `--variable`, `-v` — repeatable, **required** (no default — unlike
  `difference`, there's no "variables shared by both inputs" to fall back to,
  since the climatology's own variable names are `NAME_avg`/`NAME_std`, not
  `NAME`). Each name must be a data variable of `--input`, and both
  `NAME_avg` and `NAME_std` must be data variables of `--climatology`. All
  requested variables are checked against both inputs up front, in one pass —
  a violation on any of them exits non-zero listing everything missing from
  each side at once, not just the first problem found.
- `--output`, `-o` — output Zarr.

### Alignment

Labels are matched exactly on shared dims (e.g. `time`, `latitude`,
`longitude`) — `time` by timestamp, not by calendar day — and the result
must cover all of `--input`:

- Every `--input` label must have a climatology match. A climatology covering
  only part of the input's window or grid, sub-daily input against a daily
  climatology, or noon vs midnight stamps exits non-zero (fetch the
  climatology over the input's window and grid; `aggregate-temporal` sub-daily
  input to daily; `clip-region` / `coarsen` the input first). Extra
  climatology labels are dropped.
- The climatology may not add dims `--input` lacks (a multi-day climatology
  against a single map would broadcast the map across every day) —
  `select` one entry of the climatology first. Dims only on `--input` (e.g.
  `member`) broadcast.
- A forecast with a lead dim (`step` / `prediction_timedelta`) exits non-zero
  unless the climatology has the same dim — it would otherwise align on init
  time. Run `step-to-time` first so it aligns on valid time (and fetch the
  climatology at the matching `--prediction-timedelta`).

`clim-fetch` writes one value per calendar day at midnight (also with
`--window N`, which rolls each day's stats over N days), so a daily
`--input` stamped at midnight lines up day by day.

If both inputs have spatial dims, they are renamed onto `latitude`/`longitude`
on both sides (`--input`'s `latitude` and `--climatology`'s `lat` would
otherwise broadcast as two unrelated dims). Lat/lon values are rounded on
open, so float32 vs float64 storage of the same grid aligns.

### Units

The climatology is converted to the field's units before combining: the mean
as a value (`K` → `degree_Celsius` subtracts 273.15) and the std as a spread,
as a difference, never with an offset (a 1 K, 1 °C or 1 `delta_degC` std is
the same spread, never 274.15). So `mm day-1` against `kg m-2 s-1`, or a °C
field against a K climatology, give the same z-score.
Incompatible units (`mm day-1` against `K`), or units on only some of the
three variables, exit non-zero.

### Output

One `NAME_anomaly` variable per `--variable`, dimensionless (`units: "1"`),
`long_name` set to `"NAME standardized anomaly"`. The field's own `units`
and other attrs are **not** copied — unlike `difference`, which keeps the
first input's attrs (appropriate there since a difference of two rates is
still a rate; a z-score isn't a rate at all, so nothing physical carries
over). If a spatial dim was present, its output name is always
`latitude`/`longitude` regardless of what either input called it — see
"Alignment" above. A cell where `NAME_std` is `0` (e.g. a bone-dry precip
window with no historical variability) has an undefined z-score and is
masked to `NaN`; nothing is added to the std to avoid the division. A `NaN`
in the field, mean or std gives `NaN` in that cell; nothing is filled.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

```bash
# Standardize today's IMERG precip against its day-of-year climatology.
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py \
  --input /tmp/imerg_today.zarr --climatology /tmp/imerg_clim.zarr \
  --variable precip --output /tmp/precip_zscore.zarr

# Two variables in one call.
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py \
  --input /tmp/era5_today.zarr --climatology /tmp/era5_clim.zarr \
  --variable precip --variable t2m --output /tmp/zscores.zarr
```

### Recipe: standardizing a weekly total against its climatology

To standardize an observed weekly rainfall *total* against its climatological
weekly-total mean/std, build the climatology side with `clim-fetch`'s own
"weekly accumulation climatology" recipe (`clim-fetch --window 7` → `select`
→ `convert-to-totals`; see `clim-fetch`'s SKILL.md), then feed the result
here as `--climatology` alongside a matching `--input` (e.g. CHIRPS weekly
totals from `chirps-fetch` + `select` + `convert-to-totals`).

---
name: unit-convert
description: Convert data variables in a weather-skills standard dataset to target units (pint / CF), or normalize recognized temp/precip/pressure variables to standard display units (degree_Celsius, mm day-1, mm, hPa) via --to-standard.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# unit-convert

Converts values with **pint** / **pint-xarray** (CF/UDUNITS strings via
`cf_xarray.units`). Mass precip (`kg m-2`, `kg m-2 s-1`) converts to depth/rate
(`mm`, `mm day-1`) by dividing or multiplying by liquid-water density
(1000 kg m-3). Float32 data stays float32.

- Absolute temperatures take the offset (300 K → 26.85 `degree_Celsius`).
  Differences in `delta_degC` / `delta_degF` stay differences: `--to-standard`
  writes `delta_degC` (9 `delta_degF` → 5), and an absolute target such as
  `degC` is refused. An anomaly stored in plain `K` cannot be told apart from
  an absolute temperature — do not send it to `degC`.
- Rate ↔ amount (`mm day-1` ↔ `mm`, `kg m-2` → `mm day-1`) is refused: it needs
  a time period. Use convert-to-totals for rate → period total, deaccumulate
  for a cumulative amount → rate.
- The units attr is written in the standard spelling when the target equals
  one (`mm/day` → `mm day-1`, `degC` → `degree_Celsius`); other targets are
  written as given.
- Precip rate `standard_name` follows the target: depth rates (`mm day-1`,
  `mm hr-1`, `m s-1`) → `lwe_precipitation_rate`, mass flux → `precipitation_flux`
  — for variables with a precipitation/rainfall `standard_name`, or with none and
  a precip name (`precip`, `tp`, `pr`, ...). Any variable written in `mm`/`m` is
  stamped `lwe_thickness_of_precipitation_amount`, in `kg m-2`
  `precipitation_amount`.

## When to use

- Put two stores on one units basis before compare/concat/plot.
- Normalize temp/precip to the weather-skills display standard with `--to-standard`
  (temp → `degree_Celsius`, precip → `mm day-1`, precip amount → `mm`).

Plots already call `to_standard_units` before rendering; use this skill when you
need a converted Zarr on disk.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i <in.zarr> -o <out.zarr> \
    --to-units <UNITS> [--variable NAME] [--standard-name NAME]

uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i <in.zarr> -o <out.zarr> \
    --to-standard [--variable NAME]
```

### Arguments

- `--to-units` — target UDUNITS/CF units string (mutually exclusive with `--to-standard`).
  An unparseable string exits 2.
- `--to-standard` — convert recognized temp/precip/pressure vars (air temperature,
  precip rate/amount, sea-level and surface pressure, by CF `standard_name` or
  variable name) to standard display units.
  Other variables (SST, wind, unitless) are left unchanged and listed on stderr.
- `--variable`, `-v` — restrict to one named data var (not in the store → exit 2).
  Omit with `--to-units` to convert every data var (each must have units and
  convert to the target); omit with `--to-standard` to convert all recognized
  vars. With `--to-standard`, a named var that is not recognized exits 2 — use
  `--to-units` for it.
- `--standard-name` — override output CF `standard_name` (only with `--to-units`;
  with `--to-standard` it exits 2).

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# Flux → daily depth rate
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i /tmp/gfs.zarr -o /tmp/gfs_mm.zarr \
    --variable precipitation_surface --to-units 'mm day-1'

# Normalize everything recognized to standard display units
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i /tmp/raw.zarr -o /tmp/std.zarr \
    --to-standard
```

---
name: unit-convert
description: "Convert data variables in a weather-skills Zarr to target units (any UDUNITS / CF string), or put recognized air-temperature, precipitation and pressure variables in the standard units degree_Celsius, mm day-1, mm and hPa. Use to put two datasets on one units basis before comparing or combining them."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# unit-convert

Converts variable values and their `units` attr with pint, writing a new Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i IN.zarr -o OUT.zarr --to-units UNITS [-v NAME] [--standard-name NAME]
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i IN.zarr -o OUT.zarr --to-standard [-v NAME]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--to-units` — target units string (`'mm day-1'`, `degC`, `K`). Every selected variable must have units that convert to it. An unknown unit exits 2.
- `--to-standard` — convert recognized variables (air temperature → `degree_Celsius`, precip rate → `mm day-1`, precip amount → `mm`, sea-level and surface pressure → `hPa`, found by `standard_name` or variable name). Others (SST, wind, unitless) are left as they are and listed on stderr.
- `--variable`, `-v` — convert only this variable. Default: all variables (`--to-units`) or all recognized ones (`--to-standard`). A name not in the store, or not recognized with `--to-standard`, exits 2.
- `--standard-name` — CF `standard_name` to write; only with `--to-units`.

Pass exactly one of `--to-units`, `--to-standard`.

### Output
- Same dims and variables; float32 data stays float32.
- `units` uses the standard spelling when the target equals one (`mm/day` → `mm day-1`, `degC` → `degree_Celsius`), otherwise the string as given.
- Precip `standard_name` follows the target: depth rate → `lwe_precipitation_rate`, mass flux → `precipitation_flux`, `mm` / `m` → `lwe_thickness_of_precipitation_amount`, `kg m-2` → `precipitation_amount`.

### Notes
- Mass precip (`kg m-2`, `kg m-2 s-1`) converts to depth (`mm`, `mm day-1`) with a water density of 1000 kg m-3.
- Absolute temperatures take the offset (300 K → 26.85 °C). Temperature differences in `delta_degC` / `delta_degF` stay differences (`--to-standard` writes `delta_degC`); an absolute target such as `degC` exits 2. A difference stored in plain `K` looks absolute, so do not convert it to `degC`.
- Rate ↔ amount (`mm day-1` ↔ `mm`) exits 2: it needs a time period, which unit conversion does not supply.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i gfs_kenya.zarr -o gfs_kenya_mm.zarr \
  -v precipitation_surface --to-units 'mm day-1'
```
Writes GFS precipitation over Kenya as a depth rate in `mm day-1` with `standard_name` `lwe_precipitation_rate`.

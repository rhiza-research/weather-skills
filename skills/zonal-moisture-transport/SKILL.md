---
name: zonal-moisture-transport
description: "Compute eastward moisture flux, specific humidity times zonal wind (q·u), from a weather-skills Zarr holding both fields on pressure levels, and by default integrate it through the column to eastward integrated vapour transport (IVT, ECMWF `viwve`, kg m-1 s-1). Use for zonal moisture-transport maps or time series from forecasts or reanalysis."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py *)
metadata:
  version: "0.0.3"
  catalog-group: transforms
---

# zonal-moisture-transport

Multiplies specific humidity by zonal wind and, by default, integrates the product over pressure: `viwve = (1/g) ∫ q u dp`, with g = 9.80665 m s-2 and p in Pa.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py -i Q_AND_U.zarr [-i U.zarr] -o OUT.zarr \
  [--no-integrate] [--humidity-variable VAR] [--wind-variable VAR] [--output-name NAME]
```

### Arguments
- `--input`, `-i` — input Zarr; pass once (holding both fields) or twice (humidity first, wind second).
- `--output`, `-o` — output Zarr.
- `--humidity-variable` — specific-humidity variable. Default: the first of `q`, `specific_humidity`, `hus`, `humidity` found; with two inputs and none found, the first input's only variable.
- `--wind-variable` — zonal-wind variable. Default: the first of `u`, `wind_u`, `u_component_of_wind`, `ua`, `uwnd`, `eastward_wind` found; with two inputs and none found, the second input's only variable.
- `--integrate`, `--no-integrate` — integrate over the pressure dim, or keep per-level `q*u`. Default: integrate.
- `--output-name` — output variable name. Default: `viwve` when integrating, else `qu`.

### Output

| Mode | Variable | Units | Dims |
|---|---|---|---|
| integrate (default) | `viwve` | `kg m-1 s-1` | input dims minus the pressure dim |
| `--no-integrate` | `qu` | `kg kg-1 m s-1` | input dims |

Positive values are eastward transport. Dataset attrs come from the first input.

### Notes
- Integrating needs a pressure dim (`vertical`, `level`, `isobaricInhPa`, ...) in hPa or Pa with at least two levels that have data; otherwise it exits 2. For a single level, pass `--no-integrate`.
- The integral is trapezoidal over ascending pressure, whatever the stored level order. Units of `q` and `u` are converted to `kg kg-1` and `m s-1` from their `units` attrs.
- Humidity and wind are aligned on shared coords (inner join): only levels, times, and cells present in both are used.
- A level with no data anywhere is dropped with a `WARNING` on stderr (the ECMWF 46-day product has no humidity above 200 hPa, so its column is 1000–200 hPa). A level missing only in some columns makes those columns NaN; nothing is filled.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py -i ecmwf_s2s_q_u_kenya.zarr -o ivt_kenya.zarr
```
Writes eastward IVT for each member and lead from a 46-day ECMWF forecast of `specific_humidity` and `wind_u` on pressure levels over Kenya.

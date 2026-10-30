---
name: wind-speed-direction
description: "Compute wind speed and wind direction (the direction it blows from and the direction it blows to, in degrees clockwise from north) from the u and v wind components of a gridded or station weather-skills Zarr. Use when a task needs wind speed, a meteorological wind direction, or arrows pointing downwind."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/wind_speed_direction.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# wind-speed-direction

Turns a pair of eastward (u) and northward (v) wind components into speed and direction, on the same dims and coordinates.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/wind_speed_direction.py -i IN.zarr -o OUT.zarr --u U --v V [--calm-speed SPEED]
```

### Arguments
- `--input`, `-i` — input Zarr, gridded or station.
- `--u` — eastward wind component variable (`u10`, `u100`, or `u` on pressure levels). Required.
- `--v` — northward wind component variable (`v10`, `v100`, or `v`). Required.
- `--calm-speed` — speeds at or below this, in the units of u and v, count as calm. Default `1e-6`, which only absorbs rounding noise around zero; pass a larger value (such as `0.5` for `m s-1`) to treat light winds as calm. A negative value exits 2.
- `--output`, `-o` — output Zarr.

### Output
Three variables on the dims and coordinates of u and v; other input variables are not carried over.
- `wind_speed` — `hypot(u, v)`, in the units of u and v. CF `standard_name` `wind_speed`.
- `wind_from_direction` — degrees clockwise from north that the wind blows from (meteorological convention: a northerly is 0, a westerly 270). Units `degree`, CF `standard_name` `wind_from_direction`.
- `wind_to_direction` — degrees clockwise from north that the wind blows to (`wind_from_direction` + 180). Units `degree`, CF `standard_name` `wind_to_direction`.

### Notes
- Directions lie in [0, 360). Calm cells (speed at or below `--calm-speed`) get direction NaN; their speed is kept as computed. A NaN in u or v gives NaN in all three.
- u and v must have the same units and the same dims (for example both on pressure levels). Otherwise it exits 2; convert units with unit-convert first.
- Exits 2 when `--u` or `--v` is not in the dataset (the message lists what is).

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/wind_speed_direction.py -i gfs_kenya.zarr -o gfs_kenya_wind.zarr \
  --u wind_u_10m --v wind_v_10m
```
Writes 10 m wind speed in `m s-1` and its from / to directions over Kenya.

---
name: point-value
description: Sample any gridded weather-skills standard dataset Zarr (IMERG, CHIRPS, ERA5, SMAP, ECMWF/GEFS forecasts) at point locations and write a point_obs (station_id) dataset. Points come from a station Zarr (TAHMO, GHCN-Daily, OpenAQ), repeatable --point LAT,LON[,ID], or a CSV. Use before grid-vs-station comparison — after it, difference / verify / plot-timeseries work against the station data directly. Methods nearest (default, containing cell), bilinear, or cell-mean (N x N neighborhood). Non-spatial dims (time, step, number, vertical) are kept.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# point-value

Samples a grid at point locations and writes a CF-1.13 `timeSeries` point_obs
dataset, the same shape the station fetchers write. Run it before comparing a
grid with stations: `verify` on a raw grid + station pair broadcasts every
station against every cell.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i <grid.zarr> -o <points.zarr> \
    (--points <stations.zarr> | --point LAT,LON[,ID] ... | --points-csv <file.csv>) \
    [--method nearest|bilinear|cell-mean] [--neighborhood N] \
    [--variable VAR ...] [--max-distance DEG]
```

- `--input`, `-i` — gridded Zarr (lat/lon dims).
- Exactly one points source:
  - `--points` — a point_obs Zarr. Its point dim, lat/lon, and other 1-D
    coords (`name`, `country`, ...) are copied; its data variables are ignored.
  - `--point LAT,LON[,ID]` — repeatable; ids default to `P0`, `P1`, ....
    **Negative latitudes need `=`**: `--point=-1.29,36.82,nairobi`.
  - `--points-csv` — columns `id,lat,lon[,name]`.
- `--method` — `nearest` (default): the cell containing the point.
  `bilinear`: linear interpolation at the point (NaN if a surrounding cell is
  NaN or the point is beyond the outer cell centers). `cell-mean`:
  NaN-skipping mean of the `N x N` cells around the containing cell, clipped
  at grid edges; appends `area: mean` to `cell_methods`.
- `--neighborhood` — odd `N` for `cell-mean` (default 3).
- `--variable`, `-v` — repeatable; default all variables.
- `--max-distance` — degrees. Without it, points off the grid are NaN. With
  it, each point uses the nearest cell with any finite value within that
  distance (e.g. a coastal station by a masked cell); farther points are NaN.

Points set to NaN are listed on stderr.

## Output

- Dims `(station_id, ...)` (or the `--points` Zarr's point dim). All
  non-spatial input dims are kept.
- Variables keep names, units, and attrs, so `difference -i sampled.zarr -i
  stations.zarr` works once names match (`rename` / `unit-convert` if not).
- Coords: requested `latitude` / `longitude`, plus `grid_latitude` /
  `grid_longitude`: the center of the sampled cell.
- Provenance: standard `weather_skills_history`. With `--points`, both Zarrs
  are hashed as inputs.

## Examples

```bash
# IMERG daily at TAHMO stations
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i /tmp/imerg_daily.zarr \
    -o /tmp/imerg_at_tahmo.zarr --points /tmp/tahmo.zarr

# ECMWF ensemble at two sites (members and steps kept)
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i /tmp/s2s.zarr -o /tmp/s2s_sites.zarr \
    --point=-1.29,36.82,nairobi --point=-4.04,39.67,mombasa

# 5x5 neighborhood mean at farms, snapping to data within 0.2°
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i /tmp/smap.zarr -o /tmp/smap_farms.zarr \
    --points-csv farms.csv --method cell-mean --neighborhood 5 --max-distance 0.2
```

---
name: point-value
description: "Sample a gridded weather-skills Zarr (satellite, reanalysis, or forecast) at point locations and write a station Zarr with the same layout as station observations. Points come from a station Zarr, lat/lon pairs on the command line, or a CSV. Use to put a grid and station data on the same points before comparing them."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py *)
metadata:
  version: "0.0.3"
  catalog-group: transforms
---

# point-value

Reads grid values at given points, by nearest cell, bilinear interpolation, or a neighbourhood mean, keeping every non-spatial dim.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i GRID.zarr -o POINTS.zarr \
  (--points STATIONS.zarr | --point LAT,LON[,ID] ... | --points-csv FILE.csv) \
  [--method nearest|bilinear|cell-mean] [--neighborhood N] [-v VAR ...] [--max-distance DEG]
```

### Arguments
- `--input`, `-i` — gridded Zarr with lat/lon dims.
- `--output`, `-o` — output Zarr.
- `--points` — station Zarr. Its station dim, `latitude`/`longitude`, and other per-station coords (`name`, `country`, ...) are copied; its data variables are ignored.
- `--point` — `LAT,LON[,ID]`; repeat for several. Ids default to `P0`, `P1`, .... Example: `--point -1.29,36.82,nairobi`.
- `--points-csv` — CSV with columns `lat`, `lon`, and optional `id` and `name`.
- `--method` — `nearest`: the cell containing the point. `bilinear`: linear interpolation at the point; NaN if a surrounding cell is NaN or the point lies past the outer cell centres. `cell-mean`: NaN-skipping mean of the `N x N` cells centred on the containing cell, leaving out cells past the grid edge. Default: `nearest`.
- `--neighborhood` — odd window width `N` for `cell-mean`. Default: 3.
- `--variable`, `-v` — variable to sample; repeat for several. Default: all.
- `--max-distance` — degrees. Each point uses the nearest cell that has any finite value within this distance (for example a coastal station beside a masked sea cell); farther points are NaN. Without it, points more than half a cell from any cell centre are NaN.

Pass exactly one of `--points`, `--point`, `--points-csv`. Point ids must be unique.

### Output
- Dims `(station_id, ...)`, or the `--points` Zarr's station dim; all non-spatial input dims (`time`, `step`, `number`, `vertical`) are kept. `featureType="timeSeries"`, CF-1.13.
- Variables keep their names, units, and attrs. `cell-mean` appends `area: mean (NxN grid-cell neighborhood)` to `cell_methods`.
- Coords: the requested `latitude`/`longitude`, plus `grid_latitude`/`grid_longitude`, the centre of the cell used (units `degrees`, NaN for NaN points).
- Points set to NaN are listed on stderr (first 10).

### Notes
- Longitudes of grid and points are both wrapped to `[-180, 180)` before matching.
- With `--method bilinear`, `--max-distance` only decides which points are kept; the value is still interpolated at the point itself.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i chirps_kenya.zarr -o chirps_at_tahmo.zarr --points tahmo_kenya.zarr
```
Writes the CHIRPS value of the cell containing each TAHMO station, for every day in the grid.

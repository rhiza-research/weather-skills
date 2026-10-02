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

Extracts grid values at a set of point locations. The output is a CF-1.13
`timeSeries` point_obs dataset with a `station_id` dim (or the points
Zarr's own `point_id` / `station_id` dim), the same shape the station
fetchers write. This is the link between gridded products and station data:
once the grid is sampled at the stations, `difference`, `verify`,
`plot-timeseries`, and `plot` compare like with like.

Do not pass a gridded forecast and a station dataset to `verify` directly —
that broadcasts every station against every grid cell. Run this skill on the
grid first.

## When to use

- IMERG / CHIRPS / ERA5 rainfall at TAHMO or GHCN-Daily stations, before
  `difference` or `verify`.
- A forecast ensemble (`number`, `step`) at station sites for station
  verification or a mediogram at real station coordinates.
- Values for a list of farms or cities (CSV or `--point`).

Not for: clipping a grid to a region (`clip-region`) or regridding
(`coarsen` / `downscale`).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i <grid.zarr> -o <points.zarr> \
    (--points <stations.zarr> | --point LAT,LON[,ID] ... | --points-csv <file.csv>) \
    [--method nearest|bilinear|cell-mean] [--neighborhood N] \
    [--variable VAR ...] [--max-distance DEG]
```

### Arguments
- `--input`, `-i` — gridded Zarr with lat/lon dims.
- `--output`, `-o` — output point_obs Zarr.
- Exactly one points source:
  - `--points` — any point_obs Zarr. Its point dim, ids, lat/lon, and every
    other 1-D coord on the point dim (`name`, `country`, elevation, ...) are
    copied to the output. Its data variables are ignored.
  - `--point LAT,LON[,ID]` — repeatable ad-hoc location. Ids default to `P0`,
    `P1`, ... in order. **Negative latitudes need `=`**:
    `--point=-1.29,36.82,nairobi` (argparse reads `--point -1.29,...` as a
    flag).
  - `--points-csv` — CSV with columns `id,lat,lon[,name]`. `latitude`,
    `longitude`, `station_id`, `point_id` headers are accepted too; a missing
    id becomes `P<row>`.
- `--method`:
  - `nearest` (default) — the grid cell containing the point.
  - `bilinear` — linear interpolation at the point from the four surrounding
    cell centers. NaN if any of them is NaN or the point lies beyond the
    outermost cell centers.
  - `cell-mean` — NaN-skipping mean of the `N x N` cells centered on the
    containing cell (clipped at grid edges). Useful for representativeness
    checks. Appends `area: mean (NxN grid-cell neighborhood)` to
    `cell_methods`.
- `--neighborhood` — odd `N` for `cell-mean` (default 3). Refused with other
  methods.
- `--variable`, `-v` — repeatable; restrict to these data variables.
  Default: all.
- `--max-distance` — degrees (Euclidean in lat/lon). Without it, a point
  outside the grid (more than half a cell beyond the edge) is NaN and a point
  over a masked cell takes that cell's NaN. With it, each point samples the
  nearest cell that has any finite value (in any selected variable) within
  this distance — e.g. a coastal station next to a land-only SMAP cell — and
  points with no such cell are NaN. `bilinear` uses it only to drop points.

Points that end up NaN are listed on stderr.

### Output

- Dims `(station_id, ...)`; every non-spatial dim of the input (`time`,
  `step`, `number`, `vertical`) is kept.
- Variables keep their names, units, and attrs, so `difference -i
  sampled.zarr -i stations.zarr` works once variable names match (use
  `rename` and `unit-convert` if they do not).
- Coords on the point dim:
  - `latitude`, `longitude` — the requested point (named as in the
    `--points` Zarr when given).
  - `grid_latitude`, `grid_longitude` — center of the sampled (anchor) cell;
    for `cell-mean` the window center, for `bilinear` the nearest cell.
  - `grid_distance` — degrees from the point to that cell center.
  - Any coords copied from `--points` / the CSV `name` column.
- Dataset attrs: the input's attrs plus `featureType=timeSeries`,
  `Conventions=CF-1.13`, `point_value_method`.

Longitudes in `[0, 360]` (grid or points) are wrapped to `[-180, 180]`.

### Provenance

Standard `weather_skills_history`. With `--points` the entry is a join whose
`input` lists both the grid and the points Zarr (basename + hash). `--point`
values and the `--points-csv` path are recorded in `args`.

## Examples

```bash
# IMERG Late daily at TAHMO stations, then grid − station.
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i /tmp/imerg_daily.zarr \
    -o /tmp/imerg_at_tahmo.zarr --points /tmp/tahmo.zarr
```

```bash
# ECMWF ensemble at two sites (all members and steps kept).
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i /tmp/s2s.zarr -o /tmp/s2s_sites.zarr \
    --point=-1.29,36.82,nairobi --point=-4.04,39.67,mombasa
```

```bash
# 5x5 neighborhood mean at a CSV of farms, snapping to data within 0.2°.
uv run ${CLAUDE_SKILL_DIR}/scripts/point_value.py -i /tmp/smap.zarr -o /tmp/smap_farms.zarr \
    --points-csv farms.csv --method cell-mean --neighborhood 5 --max-distance 0.2
```

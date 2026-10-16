---
name: clip-region
description: "Spatially subset a weather-skills standard dataset Zarr to a lat/lon bbox, named country (--region), or GeoJSON polygon. Use when you need to restrict any dataset (forecast, satellite, reanalysis, stations) before downstream aggregation or plotting. --region / --geojson keep every grid cell that overlaps the polygon, including cells that straddle the country boundary."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# clip-region

Source-agnostic spatial subset of a gridded or station (point_obs) Zarr. Pass
exactly one of `--bbox`, `--region`, or `--geojson`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i <in.zarr> -o <out.zarr> --bbox N/W/S/E
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i <in.zarr> -o <out.zarr> --region Kenya
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i <in.zarr> -o <out.zarr> --geojson boundary.geojson [--keep-outside]
```

### Arguments
- `--input`, `-i` — input Zarr (gridded or point_obs).
- `--output`, `-o` — output Zarr.
- `--bbox` — `N/W/S/E` decimal degrees. Keeps grid cells whose **centre** is
  inside the box. West > east crosses the antimeridian.
- `--region` — an offline region name: ISO3 (`KEN`), country name, Natural
  Earth region (`East Africa`), custom box (`Indian Ocean`), or sub-national
  `country-admin1` (`Kenya-Turkana`). No Nominatim fallback: for a town,
  landmark, or basin, get a polygon from `resolve-region` (or another region
  skill) and pass it as `--geojson`.
- `--geojson` — path to a GeoJSON Feature, FeatureCollection (features are
  unioned), or bare geometry.
- `--keep-outside` — with `--region` / `--geojson`: keep the full grid or
  station list and set values outside the polygon to NaN.

### What `--region` / `--geojson` keep

- **Grids:** every cell whose footprint overlaps the polygon by a positive
  area, including cells that straddle the border (an edge or corner touch
  doesn't count). Overlapping cells keep their full value; nothing is
  area-weighted. Rows and columns are trimmed to the polygon's extent; inside
  that extent, cells outside the polygon are NaN.
- **Stations:** stations whose point lies inside the polygon.
- Only variables on the grid (or station axis) are masked; time bounds and
  other variables pass through unchanged. Masked integer variables become
  float (NaN needs a float).
- Country and sub-national outlines come from geoBoundaries and line up
  with each other. Multi-country regions (`East Africa`) use coarse 1:110m
  outlines, as does a country when geoBoundaries can't be reached (stderr
  warns); pass a detailed `--geojson` when those edge cells matter.

### Longitude convention

A grid stored in `[0, 360]` is wrapped to `[-180, 180)` before clipping, so
bboxes and polygons in negative longitude work. The output keeps the wrapped
longitudes.

### Output

A spatial subset of the input with a `weather_skills_history` provenance
chain (see the `provenance` skill). An empty selection exits 1 with an error.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_kenya.zarr --region Kenya

# Station point_obs: keeps stations inside the box
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i /tmp/tahmo.zarr -o /tmp/tahmo_mombasa.zarr \
    --bbox -3.92/39.57/-4.16/39.76
```

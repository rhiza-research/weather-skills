---
name: clip-region
description: "Clip a gridded or station Zarr to a lat/lon box, a named region (country, province or county, multi-country region), or a GeoJSON polygon. Use to restrict any dataset to an area of interest."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# clip-region

Keeps the part of a gridded or station dataset inside a box or polygon.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i IN.zarr -o OUT.zarr (--bbox N/W/S/E | --region NAME | --geojson FILE) [--keep-outside]
```

### Arguments
- `--input`, `-i` — input Zarr, gridded or station (stations need no time dim, e.g. after selecting one date).
- `--output`, `-o` — output Zarr.
- `--bbox` — `N/W/S/E` decimal degrees. Keeps cells whose centre is inside. West > east crosses the antimeridian.
- `--region` — ISO3 (`KEN`), country name, Natural Earth region (`East Africa`), custom box (`Indian Ocean`), or `country-admin1` (`Kenya-Turkana`). Offline lookup only.
- `--geojson` — GeoJSON Feature, FeatureCollection (unioned), or bare geometry.
- `--keep-outside` — with `--region` / `--geojson`: keep the full grid or station list and set values outside the polygon to NaN.

Pass exactly one of `--bbox`, `--region`, `--geojson`.

### Output
The input subset in space. With `--region` / `--geojson`:
- Grids keep every cell that overlaps the polygon by positive area, at full value (no area weighting), trimmed to the polygon's extent; other cells in that extent are NaN.
- Stations keep those whose point lies inside the polygon.

### Notes
- Grids stored in `[0, 360]` longitude are wrapped to `[-180, 180)`; the output keeps the wrapped longitudes.
- Masked integer variables become float. Variables off the grid or station axis pass through unchanged.
- An empty selection exits 1.
- For a statistic in each of several regions, do not clip once per region: `label-group --level admin_1` then `summarize-dim --by admin_1` writes every county's value into one dataset, in two steps.
- Multi-country regions use coarse 1:110m outlines. Pass a detailed `--geojson` when edge cells matter.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i chirps_eastafrica.zarr -o chirps_kenya.zarr --region Kenya
```
Writes CHIRPS cells that touch Kenya; cells outside the border but inside its extent are NaN.

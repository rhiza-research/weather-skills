---
name: local-extrema
description: Find the local highs or lows of a gridded weather-skills standard dataset Zarr — pressure centres in MSLP or geopotential, rainfall or temperature maxima, SST warm and cold spots — and write their locations and central values as a point_obs (station_id) dataset. One --kind (high or low) per run; --radius sets the minimum distance between centres. Non-spatial dims (time, step, number) are kept, one map at a time. Use for H / L labels on a weather map (draw the output as a text scatter layer with plot), or to list, track, or compare centres.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# local-extrema

Finds the centres of highs (`--kind high`) or lows (`--kind low`) in each map
of a gridded field and writes them as a CF-1.13 `timeSeries` point_obs
dataset, the same shape the station fetchers write.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py -i <grid.zarr> -o <centres.zarr> \
    --kind high|low [--radius DEG] [--variable VAR]
```

- `--input`, `-i` — gridded Zarr (lat/lon dims).
- `--kind` — `high` (local maxima) or `low` (local minima). Run twice for both.
- `--radius` — degrees (default 10). At most one centre of this kind within
  this distance; raise it for fewer, larger systems, lower it for more.
- `--variable`, `-v` — the field to search; required when the input has
  more than one variable.

## How a centre is found

For each map (every combination of the non-spatial dims):

1. The field is box-smoothed over a quarter of `--radius`, so small bumps
   (MSLP reduced over mountains, single noisy cells) do not count.
2. A high is a cell that is the largest smoothed value within `--radius`
   (a low, the smallest), whose window spans at least 5% of that map's
   range, so flat areas have no centres.
3. Cells closer to the map edge than the smoothing box are skipped: a field
   still rising or falling off the map has no centre there.
4. Of several centres within one radius, the strongest is kept.
5. The value written is the raw (unsmoothed) extreme under the smoothing box.

Windows do not wrap around the dateline.

## Output

- Dims `(station_id, ...)`: every grid cell that is a centre in any map is
  one point (ids `H1`, `H2`, … or `L1`, …, strongest first). Non-spatial
  input dims are kept; a point is NaN in maps where it is not a centre, so a
  moving low is a new point at each new cell. A `plot` text trace
  (`mode: "text"`) skips the NaN points.
- The variable keeps its name, units, and attrs, plus `extremum_kind` and
  `extremum_radius_deg`.
- Coords `latitude` / `longitude` (the cell centre), and any non-spatial
  coords of the input (`time`, `step`, `valid_time`, …).
- No centres: an empty `station_id` dim, with a note on stderr.

## Examples

```bash
# Highs and lows of an MSLP forecast
uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py -i /tmp/msl.zarr -o /tmp/highs.zarr --kind high
uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py -i /tmp/msl.zarr -o /tmp/lows.zarr --kind low

# Rainfall maxima at least 3 degrees apart
uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py -i /tmp/imerg_week.zarr -o /tmp/wet.zarr \
    --kind high --radius 3 --variable precip
```

To label a map, draw the output as a text scatter layer whose `texttemplate`
prints the letter and `%{customdata}` (the central value). The `plot` skill's
help has the full MSLP recipe.

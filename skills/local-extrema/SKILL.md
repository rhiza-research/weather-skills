---
name: local-extrema
description: Find the local highs or lows of a gridded weather-skills standard dataset Zarr — pressure centres in MSLP or geopotential, rainfall or temperature maxima, SST warm and cold spots — and write their locations and central values as a point_obs (station_id) dataset. One --kind (high or low) per run; --radius sets the minimum distance between centres. Non-spatial dims (time, step, number) are kept, one map at a time. Use for H / L labels on a weather map (draw the output as a text scatter layer with plot), or to list or compare centres (ids are not tracks).
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
   and single noisy cells do not count.
2. A high is a cell that is the largest smoothed value within `--radius`
   (a low, the smallest), whose window spans at least 5% of that map's
   range, so flat areas have no centres.
3. Cells whose smoothing box runs off the map edge or onto missing cells
   (NaN, e.g. land in SST) are skipped: a field still rising off the map or
   toward a coast has no centre there.
4. Of several centres within one radius, the strongest is kept.
5. The value written is the raw (unsmoothed) extreme under the smoothing box.

A global grid wraps around in longitude, so a centre on the dateline (or
on 0° for a 0–360 grid) is found once. A bbox that crosses the dateline is
searched as one contiguous map.

MSLP over high ground (Tibet, the Andes, Greenland) is reduced to sea level
and can show broad false highs and lows there that smoothing does not
remove. Treat centres over high terrain with care.

## Output

- Dims `(station_id, ...)`: every grid cell that is a centre in any map is
  one point (ids `H1`, `H2`, … or `L1`, …, strongest first). Non-spatial
  input dims are kept; a point is NaN in maps where it is not a centre. A
  centre one grid cell from a point found in an earlier map reuses that
  point (its `latitude` / `longitude` stay at the first cell), so a system
  that jitters by a cell keeps its id; one that moves further is a new
  point. A `plot` text trace
  (`mode: "text"`) skips the NaN points.
- The variable keeps its name, units, and attrs, plus `extremum_kind` and
  `extremum_radius_deg`.
- Coords `latitude` / `longitude` (the point's first cell), and any non-spatial
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

---
name: local-extrema
description: "Find the local highs or lows of a gridded field (pressure centres in MSLP or geopotential, rainfall or temperature maxima, SST warm and cold spots) and write their locations and central values as a station-style point dataset. Use for H / L labels on a weather map, or to list and compare centres."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py *)
metadata:
  version: "0.0.3"
  catalog-group: transforms
---

# local-extrema

Finds the centres of highs or lows in each map of a gridded field and writes them as points.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py -i GRID.zarr -o CENTRES.zarr --kind high|low [--radius DEG] [-v VAR]
```

### Arguments
- `--input`, `-i` — gridded Zarr (lat/lon dims).
- `--kind` — `high` (local maxima) or `low` (local minima). Run twice for both.
- `--radius` — degrees; at most one centre of this kind within this distance. Raise it for fewer, larger systems. Default: 10. For MSLP H / L labels, about 7 gives the denser set of centres a Tropical Tidbits chart marks; 15 or more misses most of them.
- `--variable`, `-v` — the field to search; required when the input has more than one variable.
- `--output`, `-o` — output Zarr.

### Output
A point_obs dataset on `(station_id, …)`, one point per cell that is a centre in any map, ids `H1`, `H2`, … (or `L1`, …), strongest first. Other input dims (time, step, number) are kept; a point is NaN in maps where it is not a centre, so drop NaN points before labelling one map. The variable keeps its name, units and attrs, plus `extremum_kind` and `extremum_radius_deg`. No centres gives an empty `station_id` dim and a note on stderr.

### Notes
- Ids are not tracks: a centre that moves more than one cell between maps becomes a new point.
- MSLP over high ground (Tibet, the Andes, Greenland) can show false highs and lows; treat centres there with care.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/local_extrema.py -i ecmwf_msl.zarr -o msl_lows.zarr --kind low --radius 8
```
Writes the low-pressure centres of each forecast step, at least 8° apart, with their central pressure.

## Details

### How a centre is found
For each map (every combination of the non-spatial dims):

1. The field is box-smoothed over a quarter of `--radius`, so small bumps and single noisy cells do not count.
2. A high is a cell that is the largest smoothed value within `--radius` (a low, the smallest), whose window spans at least 5% of that map's range, so flat areas have no centres.
3. Cells whose smoothing box runs off the map edge or onto missing cells (NaN, e.g. land in SST) are skipped.
4. Of several centres within one radius, the strongest is kept.
5. The value written is the raw (unsmoothed) extreme under the smoothing box.

A centre one cell from a point found in an earlier map reuses that point (its `latitude` / `longitude` stay at the first cell). A global grid wraps in longitude, so a centre on the dateline is found once; a bbox crossing the dateline is searched as one map.

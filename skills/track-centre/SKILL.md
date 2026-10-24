---
name: track-centre
description: "Follow one low or high (a pressure low, a tropical storm, a remnant) through each ensemble member's forecast from a starting point, and build its ensemble funnel: each member's track, the ensemble-mean track, and the circle holding 90% of members at each lead, optionally as a GeoJSON funnel polygon. Use for storm-track funnels or cones from MSLP ensembles."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/track_centre.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# track-centre

Tracks one centre through every member of a gridded ensemble forecast and writes the tracks, the mean track and the funnel.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/track_centre.py -i MSLP.zarr --start=LAT/LON -o TRACKS.zarr [--kind low|high] [--funnel 90] [--funnel-geojson FUNNEL.geojson] [--search 3] [--max-move 2.5] [--min-depth 1] [--smooth 0.5] [--min-members 15] [--every 6h] [-v VAR]
```

### Arguments
- `--input`, `-i` — gridded Zarr with a `step` (or `time`) dim and optionally `number`; any other dim must be selected first.
- `--start` — `LAT/LON` near the centre at the first lead, e.g. from `local-extrema` or an advisory. Write `--start=-5.8/49.2` for a southern latitude.
- `--kind` — `low` (default) or `high`.
- `--funnel` — percent of tracked members each funnel circle holds. Default: 90.
- `--funnel-geojson` — also write the funnel polygon as GeoJSON, for `plot --layer outline:`.
- `--search` — degrees around `--start` to look at the first lead. Default: 3.
- `--max-move` — degrees a centre may move per 6 hours. Default: 2.5.
- `--min-depth` — how far, in the variable's units (hPa for MSLP), the centre must sit below the mean of a 2–4° ring. Default: 1.
- `--smooth` — half-width in degrees of the box smoothing. Default: 0.5.
- `--min-members` — percent of members a lead needs for a funnel circle, and at least 5 (or all of a smaller ensemble). Default: 15.
- `--every` — track only at leads this far apart (a timedelta, measured from lead 0 or the first time). Default: `6h`, so hourly and 6-hourly ensembles are compared at the same leads.
- `--variable`, `-v` — required when the input has more than one variable.
- `--output`, `-o` — output Zarr.

### Output
- On `(number, step)`: `track_latitude`, `track_longitude`, the variable at the centre (e.g. `mean_sea_level_pressure`), and `depth`. NaN after a member's track ends.
- On `step`: `mean_latitude`, `mean_longitude`, `funnel_radius` (degrees of latitude) and `funnel_radius_km`, and `members`. The funnel values are NaN at leads with too few members.
- stderr gives the members still tracking the centre at each lead.

### Notes
- A track ends when the centre falls on the edge of its search window or fills to less than `--min-depth`; a weak low losing members late is the ensemble filling it, not a bug. Say how many members remain when quoting late leads.
- `--max-move` scales with the hours between tracked leads, but the window is never narrower than twice `--smooth`. The output holds only the tracked leads.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/track_centre.py -i ecmwf_msl.zarr --start=-5.8/49.2 \
  --funnel-geojson ecmwf_funnel.geojson -o ecmwf_tracks.zarr
```
Tracks a south-west Indian Ocean low through 51 ECMWF members and writes its 90% funnel.

## Details

- **Each lead.** The field is box-smoothed. At the first lead, each member's centre is the smoothed minimum (maximum, for a high) within `--search` of `--start`. At each later lead, it's within `--max-move` × hours / 6 of the previous centre, and never less than 2 cells. The written value is the raw field at that cell.
- **Depth.** `depth` is the smoothed ring mean minus the smoothed centre (the reverse for a high), using a 2–4° ring.
- **Funnel.** At each lead with enough members, the circle centred on the members' mean position holds `--funnel` percent of them. Distances are in degrees of latitude, with longitude scaled by cos(latitude). The GeoJSON polygon is the union of the hulls of consecutive circles, so it narrows and widens with the spread.
- **Grids.** Longitudes are put in [-180, 180) first; a track across the dateline is not supported.

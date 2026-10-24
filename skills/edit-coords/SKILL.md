---
name: edit-coords
description: "Rename data variables, coordinates, or dimensions in a weather-skills Zarr, or re-index a dimension by another coordinate along it. Use to make two datasets' names match before combining them, or to index stations by their ids."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py *)
metadata:
  version: "0.0.3"
  catalog-group: transforms
---

# edit-coords

Renames variables, coordinates, and dimensions, and swaps which coordinate indexes a dimension. Values and attributes are unchanged.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py -i IN.zarr -o OUT.zarr [--rename OLD=NEW ...] [--swap-dims OLD_DIM=NEW_DIM ...]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--rename` — `OLD=NEW`; rename a data variable, coordinate, or dimension. Renaming a dimension's index coordinate renames the dimension too. Two renames may swap names (`a=b` with `b=a`). Repeatable.
- `--swap-dims` — `OLD_DIM=NEW_DIM`; make the 1-D coordinate `NEW_DIM` (along `OLD_DIM`, unique values) the index of that dimension. `OLD_DIM` stays as an ordinary coordinate. Repeatable.
- `--output`, `-o` — output Zarr.

At least one of `--rename` / `--swap-dims` is required. All swaps run before all renames, and nothing is written unless every edit applies.

### Output
The input with the edits applied. Attributes are not rewritten, so a CF `bounds` or `grid_mapping` attribute naming a renamed variable keeps the old name.

### Notes
- Edits that would lose a standard dim (`lat`, `lon`, `time`, `member`, `vertical`, `point_id`, …) are refused. Renaming to an alias of the same dim (`latitude` → `lat`, `number` → `member`) is fine.
- Exits 2 when a pair is not `OLD=NEW`, `OLD` does not exist, `NEW` already exists, or a swap target is not a unique 1-D coordinate along `OLD_DIM`.
- Lead time to valid time is not a rename; use step-to-time.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py -i ifs.zarr -o ifs_precip.zarr --rename precipitation_surface=precip
```
Writes the IFS forecast with its rainfall named `precip`, matching CHIRPS and IMERG so the two can be differenced or verified by name.

## Details

Stations on an integer `station` dim with their ids in a `station_id(station)` coordinate can be indexed by id and given the standard name `point_id`:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py -i stations.zarr -o stations_r.zarr \
  --swap-dims station=station_id --rename station_id=point_id --rename station=station_name
```

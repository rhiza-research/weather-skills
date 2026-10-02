---
name: edit-coords
description: Rename data variables, coordinates, or dimensions in a weather-skills standard dataset Zarr (--rename OLD=NEW), and re-index a dimension by another 1-D coordinate along it, like xarray swap_dims (--swap-dims OLD_DIM=NEW_DIM). Use to make two datasets' names match before concat or difference, or to put stations on point_id. CF bounds and coordinates/grid_mapping references follow the rename. Refuses edits that lose an ontology dim (lat, lon, time, member, ...). To remove variables or coordinates, use drop.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# edit-coords

Name and index editing primitive. Renames any data variable, coordinate, or
dimension (`ds.rename`), and swaps which coordinate indexes a dimension
(`ds.swap_dims`), writing a new standard dataset. Values and attributes pass
through unchanged. Replaces the former `rename` skill.

## When to use

- Two datasets carry the same quantity or axis under different names
  (`precip` vs `precipitation_surface`, `latitude` vs `lat`, `number` vs
  `member`) and must match before `concat` or `difference`.
- A dimension is indexed by the wrong coordinate — e.g. stations on an integer
  `station` dim that should be indexed by their `station_id`, then named
  `point_id`.

Not for forecast lead time → valid time: that needs `time = init + step`, which
`step-to-time` computes. To remove variables or coordinates, use `drop`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py --input <in.zarr> --output <out.zarr> \
    [--swap-dims OLD_DIM=NEW_DIM ...] [--rename OLD=NEW ...]
```

### Arguments
- `--input`, `-i` — input Zarr containing a weather-skills standard dataset.
- `--output`, `-o` — output Zarr.
- `--swap-dims OLD_DIM=NEW_DIM` — repeatable. Make the existing 1-D coordinate
  `NEW_DIM` (which must lie along `OLD_DIM` and have unique values) the index of
  that dimension. `OLD_DIM` stays as an ordinary coordinate along it.
- `--rename OLD=NEW` — repeatable. Rename a data variable, coordinate, or
  dimension. Renaming a dimension's index coordinate renames the dimension
  too; a dimension without a coordinate can be renamed directly. Two renames
  may exchange names (`a=b` with `b=a`). `OLD=OLD` is a valid no-op that still
  writes a fresh provenance entry.

At least one of `--swap-dims` / `--rename` is required.

**Order:** all swaps run first, then all renames, so renames use the names as
they are after the swaps. The whole edit is checked before anything is
written: either every edit applies or the skill exits with code 2.

### Output

Same data as the input with the edits applied. When a coordinate with a CF
`bounds` variable named after it is renamed (`time` with `time_bounds`), the
bounds variable is renamed to match (`t_bounds`). Names in the `coordinates`,
`grid_mapping`, `bounds`, and `ancillary_variables` attributes are rewritten.

The skill exits with code 2 and a clear message when: a pair is not
`OLD=NEW`; a name is given twice or two edits target the same new name; `OLD`
does not exist; `NEW` already exists (and is not itself being renamed away);
the swap target is not a 1-D coordinate along `OLD_DIM` or has duplicate
values; or the edit would remove an ontology dim the input had (`lat`, `lon`,
`time`, `init_time`, `prediction_timedelta`, `member`, `vertical`,
`point_id`, ...). Renaming to an ontology alias (`latitude → lat`,
`number → member`, `step → prediction_timedelta`) keeps the dim; a
coordinate tagged with CF `standard_name: latitude` stays recognised under
any name.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `weather_skills_history` and appends its own entry. `args`
records `swap_dims` and `rename` as the lists of `OLD=NEW` strings given.

## Examples

IMERG names its precipitation `precip`; IFS names it `precipitation_surface`.
Give both a shared name so they merge:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py -i /tmp/imerg.zarr -o /tmp/imerg_r.zarr \
    --rename precip=precipitation
uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py -i /tmp/ifs.zarr -o /tmp/ifs_r.zarr \
    --rename precipitation_surface=precipitation
```

Stations on an integer `station` dim, with ids in a `station_id(station)`
coordinate — index by the ids and use the ontology name `point_id`:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/edit_coords.py -i /tmp/stations.zarr -o /tmp/stations_r.zarr \
    --swap-dims station=station_id \
    --rename station_id=point_id --rename station=station_name
```

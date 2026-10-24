---
name: label-group
description: "Add label coords to a gridded or station Zarr: the country, admin-1 / admin-2 unit (province, state, county), subregion, continent, or GeoJSON polygon each cell or station lies in, or a time label (month, year, day of year, or month groups such as MAM,OND). Data is unchanged. Use to group, select, or plot data by region or season."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# label-group

Adds label coords along space or time so a later step can group, select, or plot by them. Data variables are unchanged.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py -i IN.zarr -o OUT.zarr \
  [--level country|admin_1|admin_2|subregion|continent | --geojson FILE --property NAME] \
  [--time month|year|dayofyear|GROUPS ...]
```

### Arguments
- `--input`, `-i` — input Zarr, gridded or station.
- `--output`, `-o` — output Zarr, replaced if it exists.
- `--level` — label each cell or station with the country, admin_1, admin_2, UN subregion, or continent containing it. Country, subregion and continent outlines are bundled; admin_1 / admin_2 outlines are downloaded, as in `clip-region`.
- `--geojson` — label by the polygons of this GeoJSON FeatureCollection instead.
- `--property` — with `--geojson`: the feature property holding each polygon's label; every feature must have it.
- `--time` — repeatable. `month`, `year`, `dayofyear`, or month groups as comma-separated runs of month initials (`MAM,OND`).

Pass at least one of `--level`, `--geojson`, `--time`; `--level` and `--geojson` are mutually exclusive.

### Output
- `--level L`: coord `L` holding resolve-region keys (`kenya`, `kenya-nairobi`), plus `L_name` with display names (`Nairobi`).
- `--geojson`: coord named after `--property`.
- `--time month|year|dayofyear`: integer coord of that name along `time`.
- `--time GROUPS`: coord `month_group` along `time`.
- Anything outside every unit or group gets `""`.
- A month group crossing December takes the year it begins: Dec 2023 to Feb 2024 is `DJF`, `year` 2023.

### Notes
- A grid cell belongs to the unit containing its centre; where units overlap, it keeps the first. A station just outside every outline joins the nearest unit within 0.1°. stderr reports both.
- Labels are accepted by `clip-region --region`, except names repeated within a country, which get `_2`, `_3`, ... suffixes.
- `--time` needs a `time` dimension; run `step-to-time` first on a forecast.
- Pass the coords to `summarize-dim --by` for per-label statistics.
- Longitudes must be in -180..180.
- An existing coord of the same name is an error.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py -i chirps_kenya_daily.zarr -o chirps_kenya_labeled.zarr --level admin_1 --time year --time MAM,OND
```
Labels each CHIRPS cell with its Kenyan county and each day with its year and `MAM` or `OND` season.

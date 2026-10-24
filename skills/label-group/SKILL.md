---
name: label-group
description: "Add label coords to a gridded or station Zarr: the country, admin-1 / admin-2 unit (province, state, county), subregion, continent, or GeoJSON polygon each cell or station lies in, or a time label (month, year, day of year, or month groups such as MAM,OND). Data is unchanged. Use to group, select, or plot data by region or season."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# label-group

Writes label coords along space or time, so a later step can group, select, or plot by them.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py -i IN.zarr -o OUT.zarr \
  [--level country|admin_1|admin_2|subregion|continent | --geojson FILE --property NAME] \
  [--time month|year|dayofyear|GROUPS ...]
```

### Arguments
- `--input`, `-i` — input Zarr, gridded or station.
- `--output`, `-o` — output Zarr, replaced if it exists.
- `--level` — label each cell or station with the unit at this level that contains it. `country`, `subregion` (UN, e.g. Eastern Africa) and `continent` use bundled Natural Earth 1:50m outlines (no download; Somaliland is its own country, `somaliland`). `admin_1` / `admin_2` download geoBoundaries outlines, as `clip-region` does, about 1 MB per country for admin_1; stderr warns above 30 countries. The sources differ by a few km at coasts and borders.
- `--geojson` — label by the polygons of this GeoJSON FeatureCollection instead.
- `--property` — with `--geojson`: the feature property that holds each polygon's label. A feature without it (or with `null`) is an error; a feature with a `null` geometry labels nothing.
- `--time` — repeatable. `month`, `year`, or `dayofyear` labels each time step with that calendar part. Month groups, a comma-separated list of runs of month initials (`MAM,OND`, `DJF,MAM,JJA,SON`), label each time step with its group; groups from several flags add up.

Pass at least one of `--level`, `--geojson`, `--time`; `--level` and `--geojson` are mutually exclusive.

### Output
The input with new coords; data variables are unchanged.
- `--level L`: coord `L` on the grid's lat/lon dims or the station dim, holding resolve-region keys (`kenya`, `kenya-nairobi`, `kenya-kiambu-kiambu` for admin_2, `eastern_africa`), plus `L_name` with display names (`Nairobi`).
- `--geojson`: coord named after `--property`, holding that property as text.
- `--time month|year|dayofyear`: integer coord of that name along `time`.
- `--time GROUPS`: coord `month_group` along `time` (`MAM`).
- Cells, stations, or months outside every unit or group get `""`.
- With `year` and a group crossing December, the group's months take the year it begins: Dec 2023 to Feb 2024 are `DJF`, `year` 2023.

### Notes
- A grid cell belongs to the unit containing its centre, border included; units containing no cell centre are listed on stderr.
- A station outside every outline joins the nearest unit within 0.1° (~11 km), since simplified coastlines leave coastal stations just offshore; stderr counts them. Farther stations get `""`.
- Neighbouring admin layers overlap slightly (Ilemi Triangle: Turkana and Eastern Equatoria). A cell in two units, or on their shared border, keeps the first (by country ISO3 code for `--level`, file order for `--geojson`); stderr counts them per pair. Pass `--geojson` when border cells matter.
- Labels are resolve-region keys, accepted by `clip-region --region`. An admin_2 key names its admin_1 (`kenya-kiambu-kiambu`). A key still repeated within a country gets `_2`, `_3`, ..., numbered the same whatever the extent; those do not resolve.
- Month groups may not share a month; single initials such as `J` are refused.
- `--time` needs a `time` dimension; relabel a forecast's lead axis with `step-to-time` first.
- To summarize per label, pass the coords to `summarize-dim --by`.
- Longitudes, of the data and of `--geojson`, must be in -180..180; 0..360 is an error.
- An existing coord of the same name is an error.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py -i chirps_kenya_daily.zarr -o chirps_kenya_labeled.zarr --level admin_1 --time year --time MAM,OND
```
Labels each CHIRPS cell with its Kenyan county and each day with its year and long-rains (`MAM`) or short-rains (`OND`) season.

---
name: netcdf-io
description: "Export a weather-skills Zarr to a NetCDF (.nc) file, keeping its attributes and processing history, or read one or more NetCDF files (NetCDF-3 or NetCDF-4, such as CDS, CHIRPS or model downloads) into a weather-skills Zarr. Use when someone wants data as .nc for another tool, or has their own .nc files to compare, plot or verify."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/netcdf_io.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# netcdf-io

Moves data between a weather-skills Zarr and NetCDF, in either direction: pass exactly one of `-i` (export) or `--netcdf` (read). Every skill reads and writes Zarr, so fetch and process as usual, then export the final Zarr to `.nc`.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/netcdf_io.py -i IN.zarr -o OUT.nc [-v VAR ...]
uv run ${CLAUDE_SKILL_DIR}/scripts/netcdf_io.py --netcdf IN.nc [--netcdf IN2.nc ...] -o OUT.zarr [-v VAR ...] [--units VAR=UNITS ...]
```

### Arguments
- `--input`, `-i` — Zarr to export.
- `--netcdf` — NetCDF file to read; repeat to join several, such as one file per day.
- `--output`, `-o` — the `.nc` to write when exporting, the `.zarr` to write when reading.
- `--variable`, `-v` — keep only these data variables; repeat for several. Default: all.
- `--units` — read: `VAR=UNITS` for a variable with no `units` attribute, such as `--units tp=mm`; repeat for several. Variables without units are listed on stderr.

### Output
- Export: one NetCDF-4 file, data variables zlib-compressed. Variable and global attributes are kept, including `aggregation_period` and the Zarr's `weather_skills_history`; boolean attributes are written as 0 / 1 and other values NetCDF cannot hold as text.
- Read: the file's variables, coords and attributes as they are, with longitudes 0..360 wrapped to -180..180, a coordinate given out of order sorted, and fixed-width text (char arrays such as station names) stored as text. A coordinate value given twice exits 2. Records `weather_skills_source=netcdf:<file names>`.

### Notes
- Several `--netcdf` files are joined on their coordinate values: consecutive `time`s are stacked, and files with the same coordinates but different variables are merged. Files that overlap (a `time` in two files), share a variable at the same coordinates, or have no coordinate to order them by exit 2; read them one at a time and join with `concat` or `merge`.
- Reading keeps the file's own names: `lat`/`lon`, `valid_time` or `number` work with later skills as they are. Use `edit-coords` to rename, and `drop` for leftovers like `time_bnds`.
- Precipitation and temperature need units before later skills can use them; a file with none needs `--units`.
- The whole dataset is loaded to write the `.nc`, so export a clipped or reduced Zarr, not a raw global fetch.
- Provenance starts again at a read: the new Zarr's history begins with this skill, not with whatever made the `.nc`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/netcdf_io.py -i rfe_week_kenya.zarr -o rfe_week_kenya.nc
```
Writes a weekly Kenya rainfall Zarr as `rfe_week_kenya.nc` for use in another tool.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/netcdf_io.py --netcdf ecmwf_tp_kenya.nc -o ecmwf_tp_kenya.zarr --units tp=mm
```
Reads a downloaded ECMWF precipitation file whose `tp` has no units attribute.

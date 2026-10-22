---
name: csv-io
description: "Export a weather-skills Zarr to a CSV table (one row per time, station or grid cell, units in the headers), or read a CSV or Excel (.xlsx) sheet of station records, a gridded table, a wide table with one column per day, or a single rain gauge into a weather-skills Zarr. Use when someone wants data in a spreadsheet, or has their own CSV or Excel file, such as station rainfall, to compare, plot or verify."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# csv-io

Moves data between a weather-skills Zarr and a tidy CSV table, in either direction: pass exactly one of `-i` (export) or `--csv` (read). Reading also takes an Excel `.xlsx` file; export always writes CSV.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py -i IN.zarr -o OUT.csv [-v VAR ...] [--dropna] [--max-rows N]
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv IN.csv -o OUT.zarr [--date-format FMT] [--na-value CODE ...] [--units COLUMN=UNITS ...] [--point LAT,LON[,ID]] [--index COLUMN ...] [--sheet NAME] [--skip-rows N] [--names A,B,...] [--drop-column COLUMN ...] [--replace TEXT=NUMBER ...] [--unpivot NAME=VALUE] [-v VAR ...]
```

### Arguments
- `--input`, `-i` — Zarr to export.
- `--csv` — CSV or Excel (`.xlsx`, `.xlsm`) file to read.
- `--output`, `-o` — the `.csv` to write when exporting, the `.zarr` to write when reading.
- `--variable`, `-v` — keep only these data variables; repeat for several. Default: all.
- `--dropna` — export: skip rows where every variable is missing, such as ocean cells of a masked grid.
- `--max-rows` — export: refuse to write more rows than this. Default: 2000000.
- `--date-format` — read: strftime format of the time column. Without it only `YYYY-MM-DD` is accepted; `01/10/2026` is ambiguous, so day-first dates need `--date-format %d/%m/%Y`.
- `--na-value` — read: extra missing-value code, such as `-99`, `-999`, `T`; repeat for several. Empty cells are always missing.
- `--units` — read: `COLUMN=UNITS` for a column whose header has no units; repeat for several.
- `--point` — read: `LAT,LON[,ID]` for a single-site file with no station or lat/lon columns, such as one gauge's `date,rain`. ID defaults to `P0`. A negative latitude needs `=`: `--point=-1.29,36.82,dagoretti`.
- `--index` — read: column to use as a dim instead of detecting the layout; repeat for several.
- `--sheet` — read: Excel sheet name. Default: the first sheet.
- `--skip-rows` — read: lines to skip above the header row, such as a sheet title. Default: 0.
- `--names` — read: comma-separated names replacing the header row, one per column, for blank or unhelpful headers.
- `--drop-column` — read: column to leave out, such as a row `Total`; repeat for several.
- `--replace` — read: `TEXT=NUMBER` for a cell code, such as `tr=0` for trace rain; repeat for several. `--na-value` turns a code into missing instead.
- `--unpivot` — read: `NAME=VALUE` for a wide table. Coordinate columns (station, lat/lon, elevation, …) stay; every other column is stacked, its header into column `NAME` and its cells into `VALUE`. Non-numeric cells left in `VALUE` exit 2. Give `VALUE` units with `--units`.

### Output
- Export: columns are the dims (`time`, `station_id`, `latitude`, `longitude`, `step`, …), then other coords (station `name`, a forecast's init `time`), then variables with units in the header (`precip [mm]`, `t2m [degC]`). Dates are `YYYY-MM-DD`, with a time of day only when there is one. Leads are numbers, `step [days]` or `step [hours]`. Missing values are empty cells.
- Read, station data: dims `(station_id, time)` with `latitude`/`longitude` on `station_id`, CF `featureType=timeSeries`.
- Read, gridded data: `latitude`/`longitude` dims, sorted, longitudes in -180..180.
- Read: `weather_skills_source=csv:<file name>` and the file's sha256 in `source_csv_sha256`, for Excel files too.

### Notes
- Units come from the header, `rain [mm]` or `Rainfall (mm)`; columns with none are listed on stderr. Precipitation and temperature need units before later skills can use them.
- Column names are cleaned for CF: spaces and punctuation become `_` (`Station Name` → `Station_Name`). `--units` and `--index` take the cleaned names.
- Read steps run in this order: `--skip-rows`/`--names`, blank rows dropped, `--replace`, name cleaning, `--drop-column`, `--unpivot`, then layout detection.
- Missing values stay missing: a station/time absent from the CSV, or an empty cell, is NaN, nothing is filled. Duplicate rows for one station and time exit 2.
- A CSV keeps names, units and values only; other attrs (`aggregation_period`, `cell_methods`) and provenance are not written, so export at the end of a pipeline.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv kmd_stations.csv -o kmd_stations.zarr \
  --date-format %d/%m/%Y --na-value -99
```
Reads a met-service sheet with columns `Station,Station Name,Lat,Lon,Date,Rainfall (mm),Remarks` into a daily station Zarr of `Rainfall` in mm, with `Station_Name` on each station and `Remarks` dropped.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv oct_rain.xlsx -o oct_rain.zarr \
  --names lon,lat,station,2026-10-15,2026-10-16,2026-10-17,2026-10-18,2026-10-19,2026-10-20,2026-10-21 \
  --replace tr=0 --unpivot time=precip --units precip=mm
```
Reads a wide sheet with blank headers over its longitude, latitude and station columns and one column per day (`15th`, `16th`, …): `--names` gives every column a name and each day its date, and `--unpivot` stacks the days into a `(station_id, time)` Zarr of `precip`, with trace counted as 0 mm.

## Details

The read layout comes from the column names (case-insensitive):

| Column | Becomes |
|---|---|
| `station_id`, `station`, `point_id`, `site` | station dim `station_id` (read as text, so leading zeros stay) |
| `latitude`, `lat`, `longitude`, `lon`, `long` | per-station coords, or grid dims when there is no station column |
| `time`, `date`, `datetime`, `valid_time` | `time` dim |
| `step [days]`, `step [hours]`, `lead_time` | forecast `step` dim; a constant `time` beside it is the init |
| `number`, `member`, `realization` | ensemble `number` dim |
| `level`, `pressure` | `level` dim |

Numeric columns become data variables. Text columns constant per station (a station name) become station coords; other text columns (remarks, quality flags) are dropped with a note on stderr. Numeric per-station `altitude`/`elevation` columns are station coords.

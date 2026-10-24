---
name: csv-io
description: "Export a weather-skills Zarr to a CSV or Excel table (one row per time, station or grid cell, units in the headers, or one column per date with --pivot), or read a CSV or Excel (.xlsx) sheet of station records, a gridded table, a wide table with one column per day, or a single rain gauge into a weather-skills Zarr. Use when someone wants data in a spreadsheet, or has their own CSV or Excel file, such as station rainfall, to compare, plot or verify."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# csv-io

Moves data between a weather-skills Zarr and a tidy CSV or Excel table, in either direction: pass exactly one of `-i` (export) or `--csv` (read). The two directions mirror each other: `--na-value`, `--date-format`, `--timezone` and `--sheet` mean the same thing both ways, and `--pivot NAME=VALUE` on export writes the wide table that `--unpivot NAME=VALUE` reads.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py -i IN.zarr -o OUT.csv|OUT.xlsx [-v VAR ...] [--dropna] [--max-rows N] [--na-value CODE] [--date-format FMT] [--timezone ZONE] [--sheet NAME] [--pivot NAME=VALUE]
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv IN.csv -o OUT.zarr [--date-format FMT] [--na-value CODE ...] [--units COLUMN=UNITS ...] [--point LAT,LON[,ID]] [--index COLUMN ...] [--sheet NAME] [--skip-rows N] [--names A,B,...] [--drop-column COLUMN ...] [--replace TEXT=NUMBER ...] [--unpivot NAME=VALUE] [--timezone ZONE] [--drop-incomplete] [-v VAR ...]
```

### Arguments
- `--input`, `-i` — Zarr to export.
- `--csv` — CSV, tab-separated text or Excel (`.xlsx`, `.xlsm`) file to read.
- `--output`, `-o` — the `.csv` or `.xlsx` to write when exporting, the `.zarr` when reading.
- `--variable`, `-v` — keep only these data variables; repeatable. Default: all.
- `--dropna` — export: skip rows where every variable is missing.
- `--max-rows` — export: refuse to write more rows. Default: 2000000.
- `--date-format` — strftime format of the `time` column (other dates stay ISO). Default: `YYYY-MM-DD` (export adds ` HH:MM:SS` when there is a time of day); day-first dates need `--date-format %d/%m/%Y`. Times with an offset (`Z`, `+03:00`) are read as UTC. Export exits 2 when the format drops part of a time (`%d/%m/%Y` on hourly data). Non-standard calendars always use the default.
- `--timezone` — zone of times of day: `Africa/Nairobi`, `UTC`, `+03:00` or `UTC+3` (abbreviations such as `EAT` exit 2). Read: times without an offset are local times in it, converted to UTC; a zone in the header (`time [Africa/Nairobi]`) does the same. Export: times of day are written on its clock under `time [ZONE]`, else in UTC under `time [UTC]`. Dates alone are never moved. Default: UTC, with a note when times of day carry no zone.
- `--na-value` — missing-value code (`-99`, `T`). Read: an extra code, repeatable, matching the number however written (`-99`, `-99.0`); empty cells are always missing, and so are `NA`, `NaN`, `NULL`, `-` in a column of numbers. Codes never touch station ids, times, lat/lon, levels or members (`NA` stays Namibia), and a text code (`M`) only applies in a column of numbers, under units, or headed by a date. Export: written instead of an empty cell; once only; a code equal to a real value exits 2.
- `--units` — read: `COLUMN=UNITS` for a column whose header has none; repeatable.
- `--point` — read: `LAT,LON[,ID]` for a single-site file with no station or lat/lon columns, such as `date,rain`. ID defaults to `P0`.
- `--index` — read: column to use as a dim instead of detecting the layout; repeatable.
- `--sheet` — Excel sheet name. Default: the first visible sheet when reading (named on stderr), `Sheet1` when exporting to `.xlsx`.
- `--skip-rows` — read: lines to skip above the header row; blank lines above it are skipped anyway. Default: 0.
- `--names` — read: comma-separated names replacing the header row; none blank or repeated.
- `--drop-column` — read: column to leave out, by its header as written or cleaned (`2026-10-16` for an Excel date header); repeatable.
- `--drop-incomplete` — read: leave out rows with no station, time, level or lat/lon (footer and total rows) instead of exiting 2.
- `--replace` — read: `TEXT=NUMBER` for a cell code, such as `tr=0` for trace rain; repeatable. A column whose cells are then all numbers is read as numbers.
- `--unpivot` — read: `NAME=VALUE` for a wide table: coordinate columns, `--index` columns and text columns with no number in them under a header that is no date or number (a station name) stay; the others stack, headers into `NAME`, cells into `VALUE` (non-numeric ones exit 2). Numeric headers (steps, levels) read as numbers, Excel date headers as dates; headers mixing numbers and text (`Total`), or `NAME`/`VALUE` equal to an existing column, exit 2. The kept and stacked columns are listed on stderr.
- `--pivot` — export: `NAME=VALUE` for a wide table: one column per entry of dim `NAME`, headed by its value, cells from variable `VALUE`; the other dims stay as rows. `VALUE` must vary along `NAME`. Coords along `NAME` are dropped, and the headers carry no units, so stderr prints the command that reads it back (`--unpivot NAME=VALUE --units VALUE=UNITS`, plus `--units step=hours`, a calendar or `--timezone` when needed). A table pivoted by `station_id` has no station locations and cannot be read back.

### Output
- Export: the dims, then other coords, then variables with units in the header (`precip [mm]`). Dates are `YYYY-MM-DD`, with a time of day (and fractions of a second) only when there is one, under `time [UTC]` or `time [ZONE]`; a non-standard calendar is named in the header (`time [360_day]`) and read back as that calendar. Leads are `step [days]` or `step [hours]`; `valid_time` (time + step) is left out, or written as the init `time` when there is none. Missing dim labels (NaT) exit 2. Missing values are empty cells. Text starting `=`, `+`, `@` or `-` is written with a leading `'` so a spreadsheet does not run it, and read back without it.
- Read: station data has dims `(station_id, time)` with `latitude`/`longitude` on `station_id`, `featureType=timeSeries`; gridded data has sorted `latitude`/`longitude` dims, longitudes in -180..180; a table with only a time column (an index, an area mean) is one series along `time`. A single member or level is a scalar coord. Station longitudes are in -180..180; lat/lon must be numbers in range. Date columns beside the dims (an onset date, `init_time`) are datetime variables, or scalar coords when constant. `weather_skills_source=csv:<file name>`, sha256 in `source_csv_sha256`.

### Notes
- Units come from the header, `rain [mm]` or `Rainfall (mm)`; columns with none are listed on stderr and need `--units` for later skills.
- Column names are cleaned for CF (`Station Name` → `Station_Name`); `--units` and `--index` take the cleaned names; two headers that clean to one name exit 2. The layout comes from them ([Details](#details)).
- CSVs may be UTF-8, UTF-16 or Windows-1252, separated by `,`, tab, or `;` (then with decimal commas, `2,5`). Stray spaces around cells are dropped. Thousands separators (`1,234`) are not read.
- Excel: `.xlsx`/`.xlsm` only (`.xls`, `.ods`, `.numbers` exit 2); hidden sheets are skipped by default; formulas a program wrote without saved values exit 2 (open and save the file in Excel first). Excel keeps about 16 significant digits, so `.csv` is the lossless choice.
- Header units must be CF / UDUNITS (`mm`, `mm day-1`, `degC`, `m s-1`, `%`, `1`); `(C)`/`(F)` mean degC/degF; others exit 2 (fix them with `--units`). A note on the time format, `Date (dd/mm/yyyy)`, is ignored.
- Nothing is filled: a station/time absent from the CSV, or an empty cell, is NaN. Duplicate rows for one station and time exit 2, and so do rows with no station, time or lat/lon (unless `--drop-incomplete`) and repeated headers, including ones differing only in case. Lat/lon pairs filling under 5 % of a grid of over 10,000 cells exit 2: scattered points need a station column.
- Export takes at most 3 dims longer than 1, such as `(time, latitude, longitude)` or `(station_id, time)`; with a fourth (member, lead, level) it exits 2. Reduce it first with summarize-dim, select or aggregate-temporal. Dims no variable uses (a bounds dim such as `nv`) are left out. Variable names that would read back as something else (`level`, `lat`, `a [b]`, two that clean to one name) exit 2: rename them with edit-coords.
- A CSV keeps names, units and values only; other attrs and provenance are lost, so export at the end of a pipeline.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv kmd_stations.csv -o kmd_stations.zarr \
  --date-format %d/%m/%Y --na-value -99
```
Reads columns `Station,Station Name,Lat,Lon,Date,Rainfall (mm),Remarks` into a daily station Zarr of `Rainfall` in mm, `Station_Name` on each station, `Remarks` dropped.

## Details

The read layout comes from the column names (case-insensitive):

| Column | Becomes |
|---|---|
| `station_id`, `station`, `point_id`, `site` | station dim `station_id`, read as text so leading zeros stay; beside a `station_id` column the others are ordinary columns |
| `latitude`, `lat`, `longitude`, `lon`, `long` | per-station coords, or grid dims when there is no station column |
| `time`, `date`, `datetime`, `valid_time` | `time` dim; `time [noleap]` (any CF calendar) reads as that calendar |
| `step [days]`, `step [hours]`, `lead_time` | forecast `step` dim; a constant `time` beside it is the init |
| `number`, `member`, `realization` | ensemble `number` dim |
| `level`, `pressure_level`, `plev`, `isobaricInhPa` | `level` dim (a `pressure` column is data) |

Numeric columns become data variables. Text columns constant per station (a station name) become station coords; other text columns (remarks, quality flags) are dropped with a note on stderr. Numeric per-station `altitude`/`elevation`/`elev` columns are station coords.

Read steps run in this order: `--skip-rows`/`--names`, cells stripped, blank rows dropped, `--na-value`/`--replace`, name cleaning, `--drop-column`, `--unpivot`, times, then layout detection.

A wide sheet with blank headers over its longitude, latitude and station columns and one column per day (`15th`, `16th`, …):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv oct_rain.xlsx -o oct_rain.zarr \
  --names lon,lat,station,2026-10-15,2026-10-16,2026-10-17,2026-10-18,2026-10-19,2026-10-20,2026-10-21 \
  --replace tr=0 --unpivot time=precip --units precip=mm
```
`--names` gives every column a name and each day its date, and `--unpivot` stacks the days into a `(station_id, time)` Zarr of `precip`, with trace counted as 0 mm. `--na-value` turns a code into missing instead of a number.

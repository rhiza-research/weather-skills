---
name: csv-io
description: Export a weather-skills standard dataset Zarr to a CSV table (one row per time/station/grid cell, units in the headers), or read a CSV — station records, a gridded table, or a single rain gauge — into a standard dataset Zarr. Use when someone wants data in a spreadsheet, or has their own CSV (e.g. station rainfall) to compare, plot, or verify. Day-first dates need --date-format %d/%m/%Y; missing-value codes like -99 need --na-value.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# csv-io

Moves data between a standard dataset Zarr and a CSV table, in either
direction. Pass exactly one of `-i` (export a Zarr) or `--csv` (read a CSV).

## Usage

```
# Export
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py -i <in.zarr> -o <out.csv> \
    [--variable VAR ...] [--dropna] [--max-rows N]

# Read
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv <in.csv> -o <out.zarr> \
    [--date-format FMT] [--na-value CODE ...] [--units COLUMN=UNITS ...] \
    [--point LAT,LON[,ID]] [--index COLUMN ...] [--variable VAR ...]
```

## Export

One row per combination of the dataset's dims, in this column order:

1. the dims (`time`, `station_id`, `latitude`, `longitude`, `step`, …)
2. the other coordinates (station `name`, `latitude` / `longitude` for stations, a forecast's init `time`)
3. the data variables, with units in the header: `precip [mm]`, `t2m [degC]`

Dates are written `YYYY-MM-DD` (with a time of day only when there is one).
Forecast leads are written as numbers, `step [days]` or `step [hours]`.
Missing values are empty cells.

- `--variable`, `-v` — repeatable; default all variables.
- `--dropna` — skip rows where every variable is missing (e.g. ocean cells of
  a masked grid).
- `--max-rows` — refuse to write more rows than this (default 2,000,000). A
  full grid over many days gets big fast: shrink it first with `clip-region`,
  `select`, `aggregate-temporal`, or `point-value`.

A CSV holds names, units, and values only. Other attributes (e.g.
`aggregation_period`, `cell_methods`) and the provenance chain are not
written, so export at the end of a pipeline, not in the middle of one.

## Read

The layout comes from the column names (case-insensitive):

| Column | Becomes |
|---|---|
| `station_id`, `station`, `point_id`, `site` | station dim (`station_id`) |
| `latitude`, `lat`, `longitude`, `lon`, `long` | per-station coords, or grid dims if there is no station column |
| `time`, `date`, `datetime`, `valid_time` | `time` dim |
| `step [days]`, `step [hours]`, `lead_time` | forecast `step` dim; a constant `time` beside it is the init time |
| `number`, `member`, `realization` | ensemble `number` dim |
| `level`, `pressure` | `level` dim |

Other columns:

- **Numeric columns** become data variables.
- **Text columns that are constant per station** (e.g. a station name) become
  station coords.
- **Other text columns** (remarks, quality flags) are dropped, with a note on
  stderr.

Column names are cleaned for CF: spaces and punctuation become `_`, so
`Station Name` becomes `Station_Name`. `--units` and `--index` take the
cleaned names.

Units come from the header, as `rain [mm]` or `Rainfall (mm)`. Use
`--units COLUMN=UNITS` for columns without them; a column with no units is
listed on stderr.

- `--date-format` — strftime format of the time column. Without it, only
  `YYYY-MM-DD` (ISO 8601) dates are accepted. `01/10/2026` could be 1 October
  or 10 January, so the skill refuses to guess. For day-first dates, pass
  `--date-format %d/%m/%Y`.
- `--na-value` — repeatable extra missing-value code, e.g. `-99`, `-999`,
  `T`. Empty cells are always missing.
- `--point LAT,LON[,ID]` — for a single-site file with no station or lat/lon
  columns (e.g. one gauge's `date,rain`). ID defaults to `P0`. **Negative
  latitudes need `=`**: `--point=-1.29,36.82,dagoretti`.
- `--index COLUMN` — repeatable; set the dims yourself instead of detecting
  them.
- `--variable`, `-v` — repeatable; keep only these data variables.

Missing values stay missing. A station/time row that is absent from the CSV
is NaN in the Zarr, and nothing is filled or interpolated. Two rows for the
same station and time are an error.

The output is a standard dataset:
- station data is a CF `timeSeries` point_obs (`station_id`, `time`), the
  same shape `tahmo-fetch` and `point-value` write;
- gridded data has `latitude` / `longitude` dims, sorted, with longitudes in
  -180..180.

It stamps `weather_skills_source: csv:<file name>` and the CSV's sha256 in
`source_csv_sha256`.

## Examples

```bash
# Rainfall at stations to a spreadsheet
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py -i /tmp/chirps_at_stations.zarr \
    -o /tmp/chirps_at_stations.csv

# A met-service station sheet: Station,Station Name,Lat,Lon,Date,Rainfall (mm),Remarks
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv stations.csv -o /tmp/stations.zarr \
    --date-format %d/%m/%Y --na-value -99

# One gauge with date,rain columns
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv gauge.csv -o /tmp/gauge.zarr \
    --point=-1.30,36.75,dagoretti --units rain=mm
```

A station Zarr read this way works with `point-value --points` (sample a grid
at those stations), `difference`, `verify`, and the plot skills.

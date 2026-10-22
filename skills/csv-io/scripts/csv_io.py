# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "openpyxl>=3.1",
#   "pandas>=2.2",
#   "xarray>=2026.4",
# ]
# ///
"""Export a weather-skills Zarr to a CSV table, or read a CSV or Excel sheet into a Zarr."""

import hashlib
import re
import sys
from pathlib import Path

import cftime
import numpy as np
import pandas as pd
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# CSV header (lowercased) -> canonical coordinate name.
COLUMN_ROLES = {
    "time": "time",
    "date": "time",
    "datetime": "time",
    "valid_time": "time",
    "latitude": "latitude",
    "lat": "latitude",
    "longitude": "longitude",
    "lon": "longitude",
    "long": "longitude",
    "station_id": "station_id",
    "station": "station_id",
    "point_id": "station_id",
    "site": "station_id",
    "step": "step",
    "lead_time": "step",
    "prediction_timedelta": "step",
    "number": "number",
    "member": "number",
    "realization": "number",
    # Not "pressure": in a station table that is a measurement.
    "level": "level",
    "pressure_level": "level",
    "plev": "level",
    "isobaricinhpa": "level",
}
# Headers that name the station only when no column is called station_id; beside
# one (a 3dpaws-fetch export has station_id and site), they are ordinary columns.
STATION_ALIASES = {"station", "point_id", "site"}
COORD_ATTRS = {
    "latitude": {"standard_name": "latitude", "units": "degrees_north", "axis": "Y"},
    "longitude": {"standard_name": "longitude", "units": "degrees_east", "axis": "X"},
    "time": {"standard_name": "time", "axis": "T"},
    "step": {"standard_name": "forecast_period"},
}
STEP_UNITS = {"days": "D", "day": "D", "d": "D", "hours": "h", "hour": "h", "h": "h"}
# Numeric per-station columns that describe the site rather than measure weather.
SITE_COLUMN = re.compile(r"(latitude|longitude|altitude|elevation)$", re.IGNORECASE)
EXCEL_SUFFIXES = (".xlsx", ".xlsm")
# "rain [mm]" or "Rainfall (mm)".
HEADER_UNITS = re.compile(r"^\s*(?P<name>.+?)\s*(\[(?P<u1>[^\]]*)\]|\((?P<u2>[^)]*)\))\s*$")
# A time column of a non-standard calendar is written "time [noleap]" and read back as cftime.
CALENDARS = {
    "standard",
    "gregorian",
    "proleptic_gregorian",
    "julian",
    "noleap",
    "365_day",
    "all_leap",
    "366_day",
    "360_day",
}
CFTIME_TEXT = re.compile(r"(\d+)-(\d+)-(\d+)(?:[ T](\d+):(\d+)(?::(\d+))?)?")


def _clean_name(col):
    """Header -> (CF-safe column name, units or None): "Rain (mm)" -> ("Rain", "mm")."""
    m = HEADER_UNITS.match(str(col))
    name = m["name"] if m else str(col).strip()
    # CF lists coordinates space-separated, so names cannot hold spaces.
    name = re.sub(r"\W+", "_", name).strip("_") or "column"
    if m is None:
        return name, None
    return name, (m["u1"] if m["u1"] is not None else m["u2"]).strip()


def _export(ds, output, variable, dropna, max_rows):
    """Write ``ds`` as one tidy CSV row per coordinate combination."""
    # Short CF unit strings (mm, mm d-1) for the headers, not pint's long names.
    units = {}
    for v in ds.data_vars:
        unit = getattr(ds[v].pint, "units", None)
        units[v] = f"{unit:cf}" if unit is not None else ds[v].attrs.get("units")
        if units[v]:
            units[v] = units[v].replace("°", "deg")  # ASCII degC; Excel misreads UTF-8 °
    ds = dequantify_dataset(ds)
    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(f"--variable not in dataset: {', '.join(missing)}")
        ds = ds[variable]
    if not ds.data_vars:
        raise UsageError("dataset has no data variables to export")
    n_rows = int(np.prod([ds.sizes[d] for d in ds.dims])) if ds.dims else 1
    if n_rows > max_rows:
        raise UsageError(
            f"export would write {n_rows:,} rows (> --max-rows {max_rows:,}). "
            "Shrink it first with clip-region, select, aggregate-temporal, or point-value, "
            "or raise --max-rows."
        )

    df = ds.to_dataframe().reset_index()
    # Dims, then other coords, then variables.
    coords = [c for c in df.columns if c not in ds.dims and c not in ds.data_vars]
    df = df[[*ds.dims, *coords, *ds.data_vars]]
    if dropna:
        df = df.dropna(how="all", subset=list(ds.data_vars))

    renames = {}
    for name in df.columns:
        if name in ds.data_vars and units[name]:
            renames[name] = f"{name} [{units[name]}]"
        elif len(df) and isinstance(df[name].iloc[0], cftime.datetime):
            # cftime dates (noleap, 360_day, ...) as text, with the calendar in
            # the header so a read gets the same dates back.
            midnight = all((t.hour, t.minute, t.second) == (0, 0, 0) for t in df[name])
            fmt = "%Y-%m-%d" if midnight else "%Y-%m-%d %H:%M:%S"
            renames[name] = f"{name} [{df[name].iloc[0].calendar}]"
            df[name] = [t.strftime(fmt) for t in df[name]]
        elif pd.api.types.is_timedelta64_dtype(df[name]):
            # Leads as plain numbers so a spreadsheet can read them.
            whole_days = (df[name] % pd.Timedelta(days=1) == pd.Timedelta(0)).all()
            unit, label = ("D", "days") if whole_days else ("h", "hours")
            df[name] = df[name] / pd.Timedelta(1, unit)
            renames[name] = f"{name} [{label}]"
    df = df.rename(columns=renames)
    # float32 at its own precision: -4.025, not -4.025000095367432.
    for name in df.columns[df.dtypes == np.float32]:
        df[name] = pd.to_numeric(df[name].to_numpy().astype(str))

    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output, index=False)
    print(f"Wrote: {output} ({len(df):,} rows, {len(df.columns)} columns)", file=sys.stderr)


def _parse_point(point):
    parts = point.split(",")
    if len(parts) not in (2, 3):
        raise UsageError("--point expects LAT,LON[,ID]")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise UsageError("--point lat/lon must be numeric") from None
    return lat, lon, parts[2] if len(parts) == 3 else "P0"


def _read(
    path,
    units,
    na_value,
    date_format,
    point,
    index,
    sheet,
    names,
    skip_rows,
    replace,
    drop_column,
    unpivot,
):
    """Build a standard dataset from a tidy CSV or Excel sheet."""
    if not path.is_file():
        raise UsageError(f"CSV not found: {path}")
    if path.suffix.lower() in EXCEL_SUFFIXES:
        book = pd.ExcelFile(path, engine="openpyxl")
        if sheet is not None and sheet not in book.sheet_names:
            raise UsageError(
                f"--sheet {sheet!r} not in {path.name}; sheets: {', '.join(book.sheet_names)}"
            )
        read, opts = pd.read_excel, {"io": book, "sheet_name": sheet or 0}
    elif path.suffix.lower() == ".xls":
        raise UsageError("old-style .xls is not supported; save it as .xlsx or .csv")
    elif sheet is not None:
        raise UsageError(f"--sheet is for Excel files ({', '.join(EXCEL_SUFFIXES)})")
    else:
        read = pd.read_csv
        opts = {"filepath_or_buffer": path, "encoding": "utf-8-sig", "skipinitialspace": True}
    opts["skiprows"] = skip_rows
    header = read(**opts, nrows=0).columns
    if names:
        names = names.split(",")
        if len(names) != len(header):
            raise UsageError(f"--names has {len(names)} names for {len(header)} columns")
        opts.update(names=names, header=0)
        header = names
    # Station ids as text, so 00123 keeps its leading zeros; roles go by the cleaned
    # name ("Station ID" -> Station_ID), as below.
    cleaned = {c: _clean_name(c)[0].lower() for c in header}
    has_station_id = "station_id" in cleaned.values()
    id_cols = {
        c: str
        for c, name in cleaned.items()
        if COLUMN_ROLES.get(name) == "station_id"
        and not (has_station_id and name in STATION_ALIASES)
    }
    df = read(**opts, na_values=na_value or None, dtype=id_cols or None)
    # Spacer rows in a sheet are blank, not records.
    df = df.dropna(how="all")
    if df.empty:
        raise UsageError(f"{path} has no rows")
    for spec in replace or []:
        text, sep, value = spec.partition("=")
        try:
            value = float(value)
        except ValueError:
            raise UsageError(f"--replace expects TEXT=NUMBER, e.g. tr=0; got {spec!r}") from None
        df = df.replace(text, value)
    df = df.infer_objects()

    # "precip [mm]" -> column precip with units mm; --units fills or overrides.
    col_units, renames = {}, {}
    for col in df.columns:
        name, unit = _clean_name(col)
        renames[col] = name
        if unit is not None:
            col_units[name] = unit
    df = df.rename(columns=renames)
    for name in drop_column or []:
        if name not in df.columns:
            raise UsageError(f"--drop-column {name!r} not in columns: {', '.join(df.columns)}")
        df = df.drop(columns=name)

    # Wide table -> long: coordinate columns stay, every other column becomes rows.
    if unpivot:
        label, sep, value = unpivot.partition("=")
        if not sep or not re.fullmatch(r"\w+", label) or not re.fullmatch(r"\w+", value):
            raise UsageError(f"--unpivot expects NAME=VALUE, e.g. time=precip; got {unpivot!r}")
        keep = [c for c in df.columns if c.lower() in COLUMN_ROLES or SITE_COLUMN.search(c)]
        stacked = [c for c in df.columns if c not in keep]
        if not stacked:
            raise UsageError("--unpivot found no columns to stack beside the coordinates")
        # Stacked headers as written in the file (2026-10-15, not 2026_10_15).
        raw = {renames[c]: c for c in renames}
        df = df.melt(id_vars=keep, value_vars=stacked, var_name=label, value_name=value)
        df[label] = df[label].map(raw)
        numbers = pd.to_numeric(df[value], errors="coerce")
        bad = df.loc[numbers.isna() & df[value].notna(), value].unique()
        if len(bad):
            raise UsageError(
                f"--unpivot: non-numeric values {', '.join(map(repr, bad[:10]))}; map them "
                "with --replace TEXT=NUMBER or --na-value TEXT"
            )
        df[value] = numbers

    for spec in units or []:
        name, sep, unit = spec.partition("=")
        if not sep or name not in df.columns:
            raise UsageError(f"--units expects COLUMN=UNITS for a CSV column; got {spec!r}")
        col_units[name] = unit

    # Known coordinate headers -> canonical names (station -> station_id, date -> time, ...).
    roles = {}
    has_station_id = "station_id" in (c.lower() for c in df.columns)
    for col in df.columns:
        role = COLUMN_ROLES.get(col.lower())
        if role is None or (has_station_id and col.lower() in STATION_ALIASES):
            continue
        if role in roles.values():
            raise UsageError(f"columns {col!r} and another both look like {role}; rename one")
        roles[col] = role
    df = df.rename(columns=roles)
    col_units = {roles.get(k, k): v for k, v in col_units.items()}

    calendar = col_units.pop("time", None) if "time" in df.columns else None
    if calendar in CALENDARS:
        # "time [360_day]": dates of that calendar, as an export of cftime dates writes them.
        dates = {}
        for text in df["time"].astype(str).unique():
            m = CFTIME_TEXT.fullmatch(text.strip())
            if m is None:
                raise UsageError(f"time values like {text!r} are not YYYY-MM-DD[ HH:MM[:SS]]")
            parts = [int(x) for x in m.groups(default="0")]
            try:
                dates[text] = cftime.datetime(*parts, calendar=calendar)
            except ValueError as exc:
                raise UsageError(f"time {text!r} is not a {calendar} date ({exc})") from None
        df["time"] = df["time"].astype(str).map(dates)
    elif "time" in df.columns:
        # Without --date-format only ISO dates: 01/10/2026 is ambiguous (1 Oct or 10 Jan).
        try:
            df["time"] = pd.to_datetime(df["time"], format=date_format or "ISO8601")
        except (ValueError, TypeError):
            raise UsageError(
                f"time values like {df['time'].iloc[0]!r} are not YYYY-MM-DD; pass "
                "--date-format, e.g. --date-format %d/%m/%Y for day-first dates"
            ) from None
    if "step" in df.columns:
        unit = STEP_UNITS.get(col_units.pop("step", "days").lower())
        if unit is None:
            raise UsageError("step column units must be days or hours, e.g. 'step [days]'")
        df["step"] = pd.to_timedelta(df["step"], unit=unit)

    if point is not None:
        if "station_id" in df.columns or "latitude" in df.columns:
            raise UsageError("--point is for a single-site CSV without station or lat/lon columns")
        lat, lon, sid = _parse_point(point)
        df["station_id"], df["latitude"], df["longitude"] = sid, lat, lon

    # Dims: --index, else stations, else a lat/lon grid; time/step/member/level join them.
    if index:
        dims = [COLUMN_ROLES.get(c.lower(), c) for c in index]
        unknown = [c for c, d in zip(index, dims, strict=True) if d not in df.columns]
        if unknown:
            raise UsageError(f"--index columns not in CSV: {', '.join(unknown)}")
    else:
        extra = [d for d in ("time", "step", "number", "level") if d in df.columns]
        # A constant init time beside a step column is the forecast's scalar init.
        if "step" in extra and "time" in extra and df["time"].nunique() == 1:
            extra.remove("time")
        if "station_id" in df.columns:
            dims = ["station_id", *extra]
        elif {"latitude", "longitude"} <= set(df.columns):
            dims = [*extra, "latitude", "longitude"]
        else:
            raise UsageError(
                "cannot tell the layout: the CSV needs a station column "
                "(station_id/station/site), latitude+longitude columns, or "
                "--point LAT,LON[,ID] for a single site"
            )
    station_dim = "station_id" if "station_id" in dims else None
    if station_dim:
        df[station_dim] = df[station_dim].astype(str)

    dup = df.duplicated(subset=dims)
    if dup.any():
        row = df.loc[dup].iloc[0]
        key = ", ".join(f"{d}={row[d]}" for d in dims)
        raise UsageError(
            f"{int(dup.sum())} duplicate rows for the same {', '.join(dims)} (first: {key})"
        )

    # Other columns: station locations and per-station labels are station coords, a
    # constant time/step is a scalar coord, numbers are variables, other text is dropped.
    scalar_coords, station_coords, data_vars, dropped = {}, [], [], []
    for col in (c for c in df.columns if c not in dims):
        per_station = (
            station_dim and (df.groupby(station_dim)[col].nunique(dropna=False) <= 1).all()
        )
        numeric = pd.api.types.is_numeric_dtype(df[col])
        if station_dim and col in ("latitude", "longitude"):
            if not (per_station and numeric):
                raise UsageError(f"{col} must be a number that is constant for each station")
            station_coords.append(col)
        elif col in ("time", "step") and df[col].nunique(dropna=False) == 1:
            scalar_coords[col] = df[col].iloc[0]
        elif station_dim and per_station and numeric and SITE_COLUMN.search(col):
            station_coords.append(col)
        elif numeric:
            data_vars.append(col)
        elif per_station:
            station_coords.append(col)
        else:
            dropped.append(col)
    if station_dim and not {"latitude", "longitude"} <= set(station_coords):
        raise UsageError(
            "station CSVs need latitude and longitude columns "
            "(or --point LAT,LON[,ID] for a single site)"
        )
    if not data_vars:
        raise UsageError("no numeric data columns left after the coordinate columns")
    if dropped:
        print(f"note: dropped non-numeric columns: {', '.join(dropped)}", file=sys.stderr)

    ds = df.set_index(dims)[data_vars].to_xarray()
    if station_dim:
        first = df.groupby(station_dim, sort=True)[station_coords].first()
        first = first.reindex(ds[station_dim].values)
        ds = ds.assign_coords({c: (station_dim, first[c].values) for c in station_coords})
        ds[station_dim].attrs["cf_role"] = "timeseries_id"
        ds.attrs["featureType"] = "timeSeries"
    ds = ds.assign_coords(scalar_coords)
    if "longitude" in ds.dims:
        ds = ensure_normalized_longitude(ds, "longitude").sortby(["latitude", "longitude"])
    for name, attrs in COORD_ATTRS.items():
        if name in ds.coords:
            ds[name].attrs.update(attrs)
    if "time" in ds.coords and "step" in ds.dims:
        ds["time"].attrs["standard_name"] = "forecast_reference_time"

    missing_units = [v for v in data_vars if v not in col_units]
    for v in data_vars:
        if v in col_units:
            ds[v].attrs["units"] = col_units[v]
    if missing_units:
        print(
            f"note: no units for {', '.join(missing_units)}; "
            "put them in the header ('rain [mm]') or pass --units NAME=UNITS",
            file=sys.stderr,
        )

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"csv:{path.name}",
        source_csv_sha256=digest,
    )
    return ds


@weather_skill(name="csv-io", version=_SKILL_VERSION)
@weather_skill.argument(
    "-i", "--input", type=Dataset("any"), help="Zarr to export; -o names the .csv to write."
)
@weather_skill.argument(
    "--csv", type=Path, help="CSV or Excel (.xlsx) to read; -o names the .zarr to write."
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dropna", action="store_true", help="Export: skip rows where every variable is NaN."
)
@weather_skill.argument(
    "--max-rows", type=int, default=2_000_000, help="Export: refuse larger tables (default 2M)."
)
@weather_skill.argument(
    "--units", action="append", help="Read: COLUMN=UNITS for a column without '[units]'."
)
@weather_skill.argument(
    "--na-value", action="append", help="Read: extra missing-value marker, e.g. -999."
)
@weather_skill.argument(
    "--date-format", help="Read: strftime format of the time column, e.g. %%d/%%m/%%Y."
)
@weather_skill.argument(
    "--point",
    help="Read: LAT,LON[,ID] for a single-site CSV, e.g. -1.29,36.82,dagoretti.",
)
@weather_skill.argument(
    "--index",
    action="append",
    help="Read: column to use as a dim (repeatable; overrides detection).",
)
@weather_skill.argument("--sheet", help="Read: Excel sheet name. Default: the first sheet.")
@weather_skill.argument("--names", help="Read: comma-separated names replacing the header row.")
@weather_skill.argument(
    "--skip-rows", type=int, default=0, help="Read: lines to skip above the header row."
)
@weather_skill.argument(
    "--replace", action="append", help="Read: TEXT=NUMBER for a cell code, e.g. tr=0."
)
@weather_skill.argument("--drop-column", action="append", help="Read: column to leave out.")
@weather_skill.argument(
    "--unpivot",
    help="Read: NAME=VALUE; stack the non-coordinate columns, headers into NAME, cells into VALUE.",
)
def csv_io(
    ds,
    output,
    csv,
    variable,
    dropna,
    max_rows,
    units,
    na_value,
    date_format,
    point,
    index,
    sheet,
    names,
    skip_rows,
    replace,
    drop_column,
    unpivot,
    **kwargs,
):
    """Export a weather-skills Zarr to a CSV table, or read a CSV or Excel sheet into a Zarr."""
    if (ds is None) == (csv is None):
        raise UsageError("pass exactly one of -i/--input (Zarr to export) or --csv (CSV to read)")
    if ds is not None:
        if output.suffix.lower() != ".csv":
            raise UsageError(f"export writes CSV; -o should end in .csv (got {output})")
        read_only = (
            units,
            na_value,
            date_format,
            point,
            index,
            sheet,
            names,
            skip_rows,
            replace,
            drop_column,
            unpivot,
        )
        if any(read_only):
            raise UsageError("reading options (--units, --sheet, --unpivot, ...) are for --csv")
        _export(ds, output, variable, dropna, max_rows)
        return None
    if output.suffix.lower() == ".csv":
        raise UsageError("reading a CSV writes a Zarr; -o should not end in .csv")
    out = _read(
        csv,
        units,
        na_value,
        date_format,
        point,
        index,
        sheet,
        names,
        skip_rows,
        replace,
        drop_column,
        unpivot,
    )
    missing = [v for v in variable or [] if v not in out.data_vars]
    if missing:
        raise UsageError(
            f"--variable not in CSV: {', '.join(missing)} (have {', '.join(out.data_vars)})"
        )
    return out[variable] if variable else out


if __name__ == "__main__":
    csv_io()

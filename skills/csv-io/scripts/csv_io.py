# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "cftime>=1.6",
#   "openpyxl>=3.1",
#   "pandas>=2.2",
# ]
# ///
"""Export a weather-skills Zarr to a CSV table, or read a CSV or Excel sheet into a Zarr."""

import datetime as dt
import hashlib
import io
import re
import sys
import zipfile
import zoneinfo
from pathlib import Path

import cftime
import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import column_index_from_string
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset, units_convertible

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

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
SITE_COLUMN = re.compile(r"(latitude|longitude|altitude|elevation|(^|_)elev)$", re.IGNORECASE)
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
# Missing-value words, read as missing only in a column of numbers: as a station id or
# country, "NA" is Namibia.
NA_TEXT = {"NA", "N/A", "n/a", "#N/A", "NaN", "nan", "-nan", "NULL", "null", "None", "-"}
# "2026-10-01T03:00+03:00", "...Z", "... +0300": a time carrying its own UTC offset.
TZ_SUFFIX = re.compile(r"\s*(?:Z|[+-]\d{2}:?\d{2})$")
# A lat/lon table filling less of its grid than this, over more cells, is scattered points.
SPARSE_GRID_FILL, SPARSE_GRID_CELLS = 0.05, 10_000


def _clean_name(col):
    """Header -> (CF-safe column name, units or None): "Rain (mm)" -> ("Rain", "mm")."""
    m = HEADER_UNITS.match(str(col))
    name = m["name"] if m else str(col).strip()
    # CF lists coordinates space-separated, so names cannot hold spaces.
    name = re.sub(r"\W+", "_", name).strip("_") or "column"
    if m is None:
        return name, None
    return name, (m["u1"] if m["u1"] is not None else m["u2"]).strip()


def _parse_timezone(name):
    """``Africa/Nairobi``, ``UTC``, ``+03:00`` or ``UTC+3`` -> a tzinfo."""
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", name.strip(), re.IGNORECASE)
    if m:
        offset = dt.timedelta(hours=int(m[2]), minutes=int(m[3] or 0))
        return dt.timezone(-offset if m[1] == "-" else offset)
    if name.strip().upper() in ("UTC", "GMT", "Z"):
        return dt.UTC
    try:
        return zoneinfo.ZoneInfo(name.strip())
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise UsageError(
            f"time zone {name!r} is not known; use a name such as Africa/Nairobi or an "
            "offset such as +03:00 (abbreviations such as EAT are not read)"
        ) from None


def _export(ds, output, variable, dropna, max_rows, na_value, date_format, sheet, pivot, timezone):
    """Write ``ds`` as one tidy CSV or Excel row per coordinate combination."""
    if na_value and len(na_value) > 1:
        raise UsageError("export writes one missing-value code; pass --na-value once")
    na_rep = na_value[0] if na_value else ""
    excel = output.suffix.lower() in EXCEL_SUFFIXES
    if output.suffix.lower() == ".xlsm":
        raise UsageError("export writes .xlsx (an .xlsm needs macros Excel would refuse)")
    if output.is_dir():
        raise UsageError(f"-o {output} is a folder; give a .csv or .xlsx file name")
    if sheet is not None and not excel:
        raise UsageError(f"--sheet is for Excel output ({', '.join(EXCEL_SUFFIXES)})")
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
    # A dim no variable uses (nv under time_bounds) would repeat every row.
    unused = [d for d in ds.dims if not any(d in ds[v].dims for v in ds.data_vars)]
    if unused:
        ds = ds.drop_dims(unused)
        print(f"note: left out dims no variable uses: {', '.join(unused)}", file=sys.stderr)
    # valid_time on step is time + step; as a column it would read as a second time.
    if "step" in ds.dims and "valid_time" in ds.coords and "valid_time" not in ds.dims:
        init = ds["valid_time"] - ds["step"]
        if init.size and bool((init == init.isel({d: 0 for d in init.dims})).all()):
            if "time" not in ds.coords:
                ds = ds.assign_coords(time=init.isel({d: 0 for d in init.dims}).values)
            elif not bool((init == ds["time"]).all()):
                raise UsageError("valid_time is not time + step; rename it with edit-coords")
            ds = ds.drop_vars("valid_time")
            print("note: left out valid_time (it is time + step)", file=sys.stderr)
        else:
            raise UsageError("valid_time varies beyond time + step; rename it with edit-coords")
    # A row needs every dim value: a NaT or NaN label has nowhere to read back to.
    for d in ds.dims:
        if d in ds.coords and ds[d].isnull().any():
            raise UsageError(f"{d} has missing labels (NaT/NaN); drop them first with select")
    # Headers must read back as the same variables: not a layout name (level, lat),
    # no brackets that would read as units, and no two names that clean to one.
    seen = {_clean_name(c)[0].lower(): c for c in ds.coords}
    for c in ds.coords:
        role = COLUMN_ROLES.get(_clean_name(c)[0].lower())
        alias = "station_id" in ds.dims and _clean_name(c)[0].lower() in STATION_ALIASES
        if c not in ds.dims and role is not None and role != c and not alias:
            raise UsageError(
                f"coordinate {c!r} would read back as {role}; drop or rename it first "
                "with drop or edit-coords"
            )
    for v in ds.data_vars:
        clean = _clean_name(v)[0].lower()
        if clean in COLUMN_ROLES or HEADER_UNITS.match(str(v)) or clean in seen:
            raise UsageError(
                f"variable {v!r} would read back as "
                + (f"{seen[clean]!r}" if clean in seen else "a coordinate or units")
                + "; rename it first with edit-coords"
            )
        seen[clean] = v
    # A missing-value code equal to real values would turn them missing on read.
    if na_rep:
        try:
            code = float(na_rep)
        except ValueError:
            code = None
        clash = [
            v
            for v in ds.data_vars
            if code is not None
            and np.issubdtype(ds[v].dtype, np.number)
            and bool((ds[v] == code).any())
        ]
        if clash:
            raise UsageError(
                f"--na-value {na_rep} is a real value of {', '.join(clash)}; pick a code "
                "the data never takes (e.g. -9999), or leave missing cells empty"
            )
    # At most three dims longer than 1, such as (time, latitude, longitude); a fourth
    # (step, member, level) makes a table too long to read in a spreadsheet.
    long_dims = [d for d in ds.dims if ds.sizes[d] > 1]
    if len(long_dims) > 3:
        raise UsageError(
            f"export takes at most 3 dims; this dataset has {len(long_dims)}: "
            f"{', '.join(long_dims)}. Reduce it first: summarize-dim for an ensemble mean "
            "or an area mean, select for one member, lead or level, or aggregate-temporal."
        )
    # Long table -> wide: one column per entry of a dim, cells from one variable.
    label = value = None
    if pivot:
        label, sep, value = pivot.partition("=")
        if not sep:
            raise UsageError(f"--pivot expects NAME=VALUE, e.g. time=precip; got {pivot!r}")
        if label not in ds.dims:
            raise UsageError(f"--pivot: {label!r} is not a dim ({', '.join(ds.dims)})")
        if value not in ds.data_vars:
            raise UsageError(f"--pivot: {value!r} is not a variable ({', '.join(ds.data_vars)})")
        if label not in ds[value].dims:
            raise UsageError(f"--pivot: {value!r} does not vary along {label!r}")
        if label == "station_id":
            print(
                "note: a table pivoted by station has no station lat/lon to read back",
                file=sys.stderr,
            )
        ds = ds[[value]]
        # Coords along the pivoted dim (valid_time on step) have no column left to sit in.
        ds = ds.drop_vars([c for c in ds.coords if c != label and label in ds[c].dims])
    n_rows = int(np.prod([ds.sizes[d] for d in ds.dims])) if ds.dims else 1
    if n_rows > max_rows:
        raise UsageError(
            f"export would write {n_rows:,} rows (> --max-rows {max_rows:,}). "
            "Shrink it first with clip-region, select, aggregate-temporal, or point-value, "
            "or raise --max-rows."
        )

    if ds.dims:
        df = ds.to_dataframe().reset_index()
    else:
        # A single value (an area mean for one day) is one row.
        df = pd.DataFrame({name: [ds[name].values[()]] for name in ds.variables})
    # Dims, then other coords, then variables.
    coords = [c for c in df.columns if c not in ds.dims and c not in ds.data_vars]
    df = df[[*ds.dims, *coords, *ds.data_vars]]
    if dropna:
        df = df.dropna(how="all", subset=list(ds.data_vars))
        if df.empty:
            raise UsageError("every value is missing, so --dropna leaves no rows to write")

    renames, zones, header_units = {}, {}, {}
    for name in df.columns:
        first = df[name].dropna().iloc[0] if df[name].notna().any() else None
        if name in ds.data_vars and units[name]:
            renames[name] = f"{name} [{units[name]}]"
        elif isinstance(first, cftime.datetime):
            # cftime dates (noleap, 360_day, ...) as text, with the calendar in
            # the header so a read gets the same dates back.
            given = df[name].dropna()
            if any(t.microsecond for t in given):
                raise UsageError(f"{name} has fractions of a second, which a CSV cannot keep")
            midnight = all((t.hour, t.minute, t.second) == (0, 0, 0) for t in given)
            fmt = "%Y-%m-%d" if midnight else "%Y-%m-%d %H:%M:%S"
            header_units[name] = first.calendar
            renames[name] = f"{name} [{first.calendar}]"
            df[name] = [t.strftime(fmt) if t is not None else None for t in df[name]]
        elif pd.api.types.is_datetime64_any_dtype(df[name]):
            given = df[name].dropna()
            if (given.dt.nanosecond != 0).any():
                raise UsageError(
                    f"{name} has times finer than a microsecond, which a CSV cannot keep"
                )
            midnight = (given == given.dt.normalize()).all()
            # Seconds, or microseconds when there are fractions of a second.
            seconds = "%S.%f" if (given.dt.microsecond != 0).any() else "%S"
            # --date-format is the time column's; other dates (onset) stay ISO to read back.
            own = date_format if name == "time" else None
            fmt = own or ("%Y-%m-%d" if midnight else f"%Y-%m-%d %H:%M:{seconds}")
            times = df[name]
            if not midnight:
                # Times of day are UTC; the header says so, or names the --timezone
                # whose local clock the cells show.
                zones[name] = timezone or "UTC"
                times = times.dt.tz_localize("UTC").dt.tz_convert(_parse_timezone(zones[name]))
                renames[name] = f"{name} [{zones[name]}]"
            elif timezone:
                print(f"note: {name} holds dates only, which --timezone leaves", file=sys.stderr)
            text = times.dt.strftime(fmt)
            if own:
                # The text must give back the same clock times (no hours or fractions lost).
                back = pd.to_datetime(text, format=fmt, errors="coerce")
                wall = times.dt.tz_localize(None) if times.dt.tz is not None else times
                if (back[wall.notna()] != wall[wall.notna()]).any():
                    raise UsageError(
                        f"--date-format {date_format} drops part of the {name} values (hours "
                        f"or fractions of a second); add them, e.g. '{date_format} %H:%M'"
                    )
            if text.nunique() < df[name].nunique():
                raise UsageError(
                    f"two different {name} values would be written alike: a daylight-saving "
                    f"change in {timezone} repeats a clock time; export in UTC or a fixed "
                    "offset such as +01:00"
                )
            df[name] = text
        elif pd.api.types.is_timedelta64_dtype(df[name]):
            # Leads as plain numbers so a spreadsheet can read them.
            whole_days = (df[name] % pd.Timedelta(days=1) == pd.Timedelta(0)).all()
            unit, unit_name = ("D", "days") if whole_days else ("h", "hours")
            df[name] = df[name] / pd.Timedelta(1, unit)
            header_units[name] = unit_name
            renames[name] = f"{name} [{unit_name}]"
        elif (
            name in ds.coords
            and name not in ds.dims
            and COLUMN_ROLES.get(name.lower()) is None
            and ds[name].attrs.get("units")
            and pd.api.types.is_numeric_dtype(df[name])
        ):
            # Other numeric coords (coverage, elevation) keep their units too, unless the
            # units hold brackets a header cannot carry.
            if not re.search(r"[\[\]()]", ds[name].attrs["units"]):
                renames[name] = f"{name} [{ds[name].attrs['units']}]"
    df = df.rename(columns=renames)
    # float32 at its own precision: -4.025, not -4.025000095367432; NaN stays NaN.
    for name in df.columns[df.dtypes == np.float32]:
        df[name] = df[name].to_numpy().astype(str).astype(np.float64)
    # Text starting = + @ - would run as a formula in a spreadsheet (and openpyxl writes
    # it as one); a leading ' keeps it text, and reading drops the '.
    for name in df.columns:
        if pd.api.types.is_object_dtype(df[name]) or pd.api.types.is_string_dtype(df[name]):
            text = df[name].astype(object)
            risky = text.map(
                lambda v: isinstance(v, str) and v[:1] in ("=", "+", "@", "-", "\t", "\r")
            )
            df[name] = text.mask(risky, "'" + text.astype(str))

    if pivot:
        label_col, value_col = renames.get(label, label), renames.get(value, value)
        rows = [c for c in df.columns if c not in (label_col, value_col)]
        order = df[label_col].unique()
        df = df.pivot(index=rows, columns=label_col, values=value_col)[order]
        df.columns = [str(c) for c in df.columns]
        df = df.reset_index()
        # Headers now hold the dim's values, so the units (or time zone) of both go nowhere.
        hint = f"--unpivot {label}={value} --units {value}={units[value] or 'UNITS'}"
        if label in header_units:
            # Step units (hours, days) or a calendar (360_day) ride on --units too.
            hint += f" --units {label}={header_units[label]}"
        if label in zones:
            hint += f" --timezone {zones[label]}"
        if label != "station_id":
            print(
                f"note: the pivoted CSV has no units for {value} or {label}; read it back "
                f"with {hint}",
                file=sys.stderr,
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    if excel:
        if len(df) >= 1_048_576 or len(df.columns) > 16_384:
            raise UsageError(f"{len(df):,} rows x {len(df.columns):,} columns is too big for Excel")
        for name in df.columns:
            if not (
                pd.api.types.is_object_dtype(df[name]) or pd.api.types.is_string_dtype(df[name])
            ):
                continue
            text = df[name].astype(object)
            if text.map(lambda v: isinstance(v, str) and len(v) > 32_767).any():
                raise UsageError(f"{name} has text longer than Excel's 32,767 characters; use .csv")
            # Control characters (other than tab and newlines) are not allowed in a sheet.
            df[name] = text.map(
                lambda v: ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v
            )
        df.to_excel(output, sheet_name=sheet or "Sheet1", index=False, na_rep=na_rep)
    else:
        df.to_csv(output, index=False, na_rep=na_rep)
    print(f"Wrote: {output} ({len(df):,} rows, {len(df.columns)} columns)", file=sys.stderr)


def _parse_point(point):
    parts = point.split(",")
    if len(parts) not in (2, 3):
        raise UsageError("--point expects LAT,LON[,ID]")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise UsageError("--point lat/lon must be numeric") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 360):
        raise UsageError(f"--point {point}: latitude must be in -90..90, longitude in -180..360")
    sid = parts[2].strip() if len(parts) == 3 else "P0"
    if not sid:
        raise UsageError(f"--point {point}: the station id after the second comma is empty")
    return lat, lon, sid


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
    timezone,
    drop_incomplete,
):
    """Build a standard dataset from a tidy CSV or Excel sheet."""
    if not path.is_file():
        raise UsageError(f"CSV not found: {path}")
    if skip_rows < 0:
        raise UsageError(f"--skip-rows must be 0 or more; got {skip_rows}")
    suffix = path.suffix.lower()
    sheets = []
    if suffix in EXCEL_SUFFIXES:
        try:
            book = pd.ExcelFile(path, engine="openpyxl")
        except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
            raise UsageError(f"{path} is not a valid Excel workbook ({exc})") from None
        # Hidden sheets hold lookups or stale copies; the default is the first visible one.
        sheets = [ws.title for ws in book.book.worksheets if ws.sheet_state == "visible"]
        sheets = sheets or book.sheet_names
        if sheet is not None and sheet not in book.sheet_names:
            raise UsageError(
                f"--sheet {sheet!r} not in {path.name}; sheets: {', '.join(book.sheet_names)}"
            )
        if sheet is None and len(book.sheet_names) > 1:
            print(
                f"note: reading sheet {sheets[0]!r}; pass --sheet for another "
                f"({', '.join(book.sheet_names)})",
                file=sys.stderr,
            )
        sheet = sheet or sheets[0]
        read, opts = pd.read_excel, {"io": book, "sheet_name": sheet}
        # Formulas a program wrote and Excel never computed have no saved value (<f>
        # with no <v>); pandas would read them as empty. Found here, counted once the
        # header and dropped columns are known.
        part = load_workbook(path, read_only=True)[sheet]._worksheet_path
        with zipfile.ZipFile(path) as archive:
            xml = archive.read(part)
        # A formula cell with no value, or an empty one (<v />), unless its result is
        # text (t="str"), where empty is the saved result of IF(..., "").
        cells = rb'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)'
        uncached = [
            (int(m[2]) - 1, column_index_from_string(m[1].decode()) - 1)
            for m in re.finditer(cells, xml, re.S)
            if m[4]
            and b"<f" in m[4]
            and b't="str"' not in m[3]
            and not re.search(rb"<v>[^<]+</v>", m[4])
        ]
    elif suffix in (".xls", ".ods", ".numbers"):
        raise UsageError(f"{suffix} files are not read; save it as .xlsx or .csv")
    elif sheet is not None:
        raise UsageError(f"--sheet is for Excel files ({', '.join(EXCEL_SUFFIXES)})")
    else:
        data = path.read_bytes()
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            # Excel's "Unicode text" export: UTF-16, tab-separated.
            text = data.decode("utf-16")
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                # Excel on Windows saves "CSV" as Windows-1252, not UTF-8.
                text = data.removeprefix(b"\xef\xbb\xbf").decode("cp1252", errors="replace")
                print(
                    f"note: {path.name} is not UTF-8; reading it as Windows-1252",
                    file=sys.stderr,
                )
        lines = text.splitlines()
        # Blank lines above the header are skipped, as spreadsheets often leave one.
        while skip_rows < len(lines) and not lines[skip_rows].strip():
            skip_rows += 1
        if skip_rows >= len(lines):
            raise UsageError(f"{path} has no header row (after --skip-rows {skip_rows})")
        # The separator is whichever of , ; or tab the header row has most of.
        sep = max([",", ";", "\t"], key=lines[skip_rows].count)

        def read(**kwargs):
            return pd.read_csv(io.StringIO(text), **kwargs)

        opts = {"skipinitialspace": True, "sep": sep}
        if sep == ";":
            # European spreadsheets write ; between cells and a decimal comma (2,5).
            opts["decimal"] = ","
            print("note: reading ;-separated cells with decimal commas", file=sys.stderr)
    if suffix in EXCEL_SUFFIXES:
        top = read(**opts, header=None, nrows=100, skiprows=skip_rows)
        filled = top.notna().any(axis=1).to_numpy()
        skip_rows += int(filled.argmax()) if filled.any() else 0
    plain_read = read

    def read(**kwargs):
        try:
            return plain_read(**kwargs)
        except OverflowError as exc:
            raise UsageError(f"{path} has a number too large to read ({exc})") from None

    opts["skiprows"] = skip_rows
    try:
        first_row = read(**opts, header=None, nrows=1, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        raise UsageError(f"{path} has no header row (after --skip-rows {skip_rows})") from None
    if first_row.empty:
        others = [s for s in sheets if s != sheet]
        hint = f"; other sheets: {', '.join(others)}" if others else ""
        raise UsageError(f"{path} has no header row (after --skip-rows {skip_rows}){hint}")
    # Headers as written: pandas would rename a repeated one to "850.1".
    header = [str(h).strip() for h in first_row.iloc[0]]
    if names:
        names = [n.strip() for n in names.split(",")]
        if not all(names):
            raise UsageError("--names has an empty name; give every column one")
        if len(names) != len(header):
            raise UsageError(f"--names has {len(names)} names for {len(header)} columns")
        opts.update(names=names, header=0)
        header = names
    # Blank header cells (read as "" or "nan") may repeat; named ones may not.
    named = [h for h in header if h not in ("", "nan")]
    # 1 and 1.0 are one level or step.
    keys = [float(h) if re.fullmatch(r"[+-]?\d+(\.\d*)?", h) else h for h in named]
    repeated = pd.Series(keys, dtype=object).duplicated()
    if repeated.any():
        twice = named[repeated.idxmax()]
        raise UsageError(
            f"column {twice!r} appears twice in the header; rename one with --names, or "
            "fix the header in the file"
        )
    header = read(**opts, nrows=0).columns if not names else names
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
    # Only empty cells and --na-value codes are missing here; NA_TEXT words are dropped
    # below, in number columns only.
    try:
        df = read(**opts, keep_default_na=False, na_values=[""], dtype=id_cols or None)
    except OverflowError as exc:
        raise UsageError(f"{path} has a number too large to read ({exc})") from None
    if suffix in EXCEL_SUFFIXES:
        dropped_raw = {str(c).strip() for c in drop_column or []}
        unread = [
            (row, col)
            for row, col in uncached
            if row > skip_rows
            and col < len(header)
            and str(header[col]).strip() not in dropped_raw
            and _clean_name(header[col])[0] not in dropped_raw
        ]
        if unread:
            raise UsageError(
                f"{len(unread):,} formula cells in sheet {sheet!r} have no saved value (the "
                "file was written by a program, not saved by Excel); open and save it in "
                "Excel or LibreOffice first"
            )
    # Cells without stray spaces: "T " hides a code and "A " splits a station.
    for col in df.columns:
        if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
            stripped = df[col].map(lambda v: v.strip() if isinstance(v, str) else v)
            # Export writes '=x for text starting = + @, so a spreadsheet keeps it text.
            stripped = stripped.map(
                lambda v: v[1:] if isinstance(v, str) and v[:2] in ("'=", "'+", "'@", "'-") else v
            )
            df[col] = stripped.astype(object).mask(stripped == "")
    # Spacer rows in a sheet are blank, not records.
    df = df.dropna(how="all")
    if df.empty:
        raise UsageError(f"{path} has no rows")
    # --na-value and --replace codes mark measurements, never station ids, times, lat/lon,
    # levels or members; a numeric code also matches the number however it is written.
    codes = [(code.strip(), np.nan) for code in na_value or []]
    for spec in replace or []:
        code, sep, number = spec.partition("=")
        try:
            codes.append((code.strip(), float(number)))
        except ValueError:
            raise UsageError(f"--replace expects TEXT=NUMBER, e.g. tr=0; got {spec!r}") from None
        if not code.strip():
            raise UsageError(f"--replace needs the text to replace, e.g. tr=0; got {spec!r}")
    for code, number in codes:
        try:
            as_number = float(code)
        except ValueError:
            as_number = None
        for col in df.columns:
            role = COLUMN_ROLES.get(_clean_name(col)[0].lower())
            if col in id_cols or role is not None or SITE_COLUMN.search(_clean_name(col)[0]):
                continue
            hit = df[col].astype(object) == code
            if as_number is not None:
                hit |= pd.to_numeric(df[col], errors="coerce") == as_number
            # A text code ("M") is a missing value in a column of numbers, or one whose
            # header has units; in a column of names it is a name.
            rest = pd.to_numeric(
                df[col].mask(hit | df[col].isin(NA_TEXT)).dropna(), errors="coerce"
            )
            unit = _clean_name(col)[1]
            # A wide table's value columns are headed by a date or a number.
            stacked_header = isinstance(col, dt.date) or not (
                pd.isna(pd.to_numeric(str(col), errors="coerce"))
                and pd.isna(pd.to_datetime(str(col), format="ISO8601", errors="coerce"))
            )
            measured = (
                rest.notna().any()
                or stacked_header
                or (unit is not None and units_convertible(unit, unit))
            )
            if as_number is None and (rest.isna().any() or not measured):
                continue
            if hit.any():
                df[col] = df[col].astype(object).mask(hit, number)
    df = df.infer_objects()
    # One text cell ("tr" before --replace, an Excel number stored as text, "NA") makes a
    # whole column text; read it as numbers when every other cell is one.
    for col in df.columns:
        if col in id_cols or not (
            pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col])
        ):
            continue
        cells = df[col].mask(df[col].isin(NA_TEXT))
        numbers = pd.to_numeric(cells, errors="coerce")
        if numbers.notna().any() and (numbers.notna() == cells.notna()).all():
            df[col] = numbers
    # A trailing comma or a blank header cell over an empty column is not a variable.
    blank = [c for c in df.columns if str(c).startswith("Unnamed:") and df[c].isna().all()]
    df = df.drop(columns=blank)

    # "precip [mm]" -> column precip with units mm; --units fills or overrides.
    col_units, renames = {}, {}
    for col in df.columns:
        name, unit = _clean_name(col)
        renames[col] = name
        if unit is not None:
            col_units[name] = unit
    # Zarr writes one folder per variable, so Rain and rain would collide on macOS.
    cleaned = pd.Series(list(renames.values()))
    if cleaned.str.lower().duplicated().any():
        raise UsageError(
            f"two columns both clean to {cleaned[cleaned.str.lower().duplicated()].iloc[0]!r} "
            "(ignoring case); rename one with --names"
        )
    df = df.rename(columns=renames)
    written = {str(raw).strip(): clean for raw, clean in renames.items()}
    for raw, clean in renames.items():
        if isinstance(raw, dt.datetime) and raw == pd.Timestamp(raw).normalize():
            written[raw.strftime("%Y-%m-%d")] = clean
    for name in drop_column or []:
        name = written.get(name.strip(), name)
        if name not in df.columns:
            raise UsageError(f"--drop-column {name!r} not in columns: {', '.join(df.columns)}")
        df = df.drop(columns=name)

    # Wide table -> long: coordinate columns stay, every other column becomes rows.
    if unpivot:
        label, sep, value = unpivot.partition("=")
        if not sep or not re.fullmatch(r"\w+", label) or not re.fullmatch(r"\w+", value):
            raise UsageError(f"--unpivot expects NAME=VALUE, e.g. time=precip; got {unpivot!r}")
        if label.lower() == value.lower():
            raise UsageError(f"--unpivot NAME and VALUE must differ; got {unpivot!r}")
        # Stacked headers as written in the file (2026-10-15, not 2026_10_15).
        raw = {renames[c]: c for c in renames}
        keep = []
        for c in df.columns:
            header = raw[c]
            if c.lower() in COLUMN_ROLES or SITE_COLUMN.search(c) or c in (index or []):
                keep.append(c)
            elif pd.api.types.is_numeric_dtype(df[c]) or isinstance(header, dt.date):
                continue
            # A text column under a header that is no date or number, with no cell a
            # number (a station name, a country), labels the rows.
            elif (
                pd.isna(pd.to_numeric(str(header), errors="coerce"))
                and pd.isna(pd.to_datetime(str(header), format="ISO8601", errors="coerce"))
                and pd.to_numeric(df[c].mask(df[c].isin(NA_TEXT)), errors="coerce").isna().all()
                and df[c].mask(df[c].isin(NA_TEXT)).notna().any()
            ):
                keep.append(c)
        stacked = [c for c in df.columns if c not in keep]
        if not stacked:
            raise UsageError("--unpivot found no columns to stack beside the coordinates")
        # The new columns must not take the name or role of a column that stays.
        kept_roles = {COLUMN_ROLES.get(c.lower(), c.lower()) for c in df.columns}
        for new in (label, value):
            if COLUMN_ROLES.get(new.lower(), new.lower()) in kept_roles:
                raise UsageError(
                    f"--unpivot {unpivot}: {new!r} is already a column (or its role); pick "
                    "another name"
                )
        print(
            f"note: --unpivot keeps {', '.join(keep) or 'no columns'} and stacks "
            f"{len(stacked)} columns ({stacked[0]} ... {stacked[-1]})",
            file=sys.stderr,
        )
        df = df.melt(id_vars=keep, value_vars=stacked, var_name=label, value_name=value)
        df[label] = df[label].map(raw)
        # Numeric headers (steps, levels, members) are numbers, not text; Excel date
        # headers stay dates.
        labels = pd.to_numeric(df[label].astype(str), errors="coerce")
        if labels.notna().all():
            df[label] = labels
        elif labels.notna().any():
            text = sorted({str(h) for h in df.loc[labels.isna(), label]})
            raise UsageError(
                f"--unpivot: headers mix numbers and text ({', '.join(text[:5])}); leave "
                "the text ones out with --drop-column"
            )
        cells = df[value].mask(df[value].isin(NA_TEXT))
        numbers = pd.to_numeric(cells, errors="coerce")
        bad = df.loc[numbers.isna() & cells.notna(), value].unique()
        if len(bad):
            raise UsageError(
                f"--unpivot: non-numeric values {', '.join(map(repr, bad[:10]))}; map them "
                "with --replace TEXT=NUMBER or --na-value TEXT"
            )
        df[value] = numbers

    # "Temp (C)", "Temp (F)": degrees, not coulombs or farads.
    for name, unit in col_units.items():
        if unit in ("C", "F", "°C", "°F", "deg C", "deg F"):
            col_units[name] = "degC" if unit.endswith("C") else "degF"
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
    if calendar in CALENDARS and timezone:
        raise UsageError(f"--timezone is for standard-calendar times, not {calendar}")
    if calendar in CALENDARS:
        # "time [360_day]": dates of that calendar, as an export of cftime dates writes them.
        dates = {}
        for text in df["time"].dropna().astype(str).unique():
            m = CFTIME_TEXT.fullmatch(text.strip())
            if m is None:
                raise UsageError(f"time values like {text!r} are not YYYY-MM-DD[ HH:MM[:SS]]")
            parts = [int(x) for x in m.groups(default="0")]
            try:
                dates[text] = cftime.datetime(*parts, calendar=calendar)
            except ValueError as exc:
                raise UsageError(f"time {text!r} is not a {calendar} date ({exc})") from None
        df["time"] = df["time"].map(lambda v: dates[str(v)] if pd.notna(v) else None)
    elif "time" in df.columns:
        # The zone of times without an offset: --timezone, or the header ("time [UTC]",
        # "time [Africa/Nairobi]", as export writes them).
        if calendar and (re.search(r"(?i)(dd|mm|yy|hh)", calendar) or calendar.isdigit()):
            # "Date (dd/mm/yyyy)": a note on the format, not a zone.
            print(f"note: ignoring ({calendar}) in the time header", file=sys.stderr)
            calendar = None
        if timezone and calendar and _parse_timezone(calendar) != _parse_timezone(timezone):
            raise UsageError(f"--timezone {timezone} but the time header says [{calendar}]")
        zone = _parse_timezone(timezone or calendar) if (timezone or calendar) else None
        given = df["time"].notna()
        if date_format:
            has_offset = given & ("%z" in date_format)
        else:
            has_offset = given & df["time"].astype(str).str.contains(TZ_SUFFIX)
        if has_offset.any() and not has_offset[given].all():
            first = df.loc[given & ~has_offset, "time"].iloc[0]
            raise UsageError(
                f"time values mix UTC offsets (+03:00, Z) with none ({first!r}); "
                "give every time an offset, or none and pass --timezone"
            )
        # Without --date-format only ISO dates: 01/10/2026 is ambiguous (1 Oct or 10 Jan).
        times = pd.to_datetime(
            df["time"], format=date_format or "ISO8601", errors="coerce", utc=True
        )
        bad = df.loc[times.isna() & given, "time"]
        if len(bad):
            expected = f"in --date-format {date_format}" if date_format else "YYYY-MM-DD"
            hint = (
                "if a header is no date (Total, 15th), leave it out with --drop-column or "
                "name the dates with --names"
                if unpivot
                else "time-zone names such as EAT are not read: drop them and pass --timezone"
            )
            raise UsageError(
                f"time values like {bad.iloc[0]!r} are not {expected}; pass --date-format, "
                f"e.g. --date-format %d/%m/%Y for day-first dates ({hint})"
            )
        if date_format and not re.search(r"%[yYGcxsF]", date_format.replace("%%", "")):
            # "%b-%d" alone would date everything in 1900.
            raise UsageError(f"--date-format {date_format} has no year (%Y or %y)")
        if has_offset.any():
            # Each time's own clock, without its offset: dates at local midnight
            # (daily records) keep their local date instead of moving to 21:00 UTC.
            local = pd.to_datetime(
                df["time"].astype(str).str.replace(TZ_SUFFIX, "", regex=True),
                format=date_format.replace("%z", "").strip() if date_format else "ISO8601",
                errors="coerce",
            )
            daily = (local[given] == local[given].dt.normalize()).all()
            df["time"] = local if daily else times.dt.tz_localize(None)
            if daily:
                print("note: dates at local midnight are read as those dates", file=sys.stderr)
        else:
            df["time"] = times.dt.tz_localize(None)
            daily = (df["time"][given] == df["time"][given].dt.normalize()).all()
            if zone is not None and not daily:
                try:
                    df["time"] = (
                        df["time"]
                        .dt.tz_localize(zone, ambiguous="raise", nonexistent="raise")
                        .dt.tz_convert("UTC")
                        .dt.tz_localize(None)
                    )
                except ValueError as exc:
                    # A clock time skipped or repeated by a daylight-saving change.
                    detail = re.sub(r",? ?[Tt]ry using .*$|\. .*$", "", str(exc))
                    raise UsageError(
                        f"a daylight-saving change in {zone} skips or repeats a time in the "
                        f"file ({detail}); give such times a UTC offset"
                    ) from None
            elif zone is None and not daily:
                print(
                    "note: times of day without an offset are read as UTC; pass --timezone "
                    "(e.g. Africa/Nairobi) if they are local times",
                    file=sys.stderr,
                )
    if "step" in df.columns:
        unit = STEP_UNITS.get(col_units.pop("step", "days").lower())
        if unit is None:
            raise UsageError("step column units must be days or hours, e.g. 'step [days]'")
        steps = pd.to_numeric(df["step"], errors="coerce")
        bad = df.loc[steps.isna() & df["step"].notna(), "step"]
        if len(bad):
            raise UsageError(
                f"step values must be numbers of days or hours; got {bad.iloc[0]!r}"
                + (" (leave a Total header out with --drop-column)" if unpivot else "")
            )
        df["step"] = pd.to_timedelta(steps, unit=unit)

    if point is not None:
        if "station_id" in df.columns or "latitude" in df.columns:
            raise UsageError("--point is for a single-site CSV without station or lat/lon columns")
        lat, lon, sid = _parse_point(point)
        df["station_id"], df["latitude"], df["longitude"] = sid, lat, lon

    # Dims: --index, else stations, else a lat/lon grid; time/step/member/level join them.
    if index:
        dims = [COLUMN_ROLES.get(c.lower(), c) for c in index]
        if len(set(dims)) < len(dims):
            raise UsageError(f"--index names one column twice: {', '.join(index)}")
        unknown = [c for c, d in zip(index, dims, strict=True) if d not in df.columns]
        if unknown:
            raise UsageError(f"--index columns not in CSV: {', '.join(unknown)}")
    else:
        extra = [d for d in ("time", "step", "number", "level") if d in df.columns]
        # A constant init time beside a step column is the forecast's scalar init; one
        # member or level is a scalar, as select leaves it.
        if "step" in extra and "time" in extra and df["time"].nunique() == 1:
            extra.remove("time")
        extra = [d for d in extra if not (d in ("number", "level") and df[d].nunique() == 1)]
        if "station_id" in df.columns:
            dims = ["station_id", *extra]
        elif {"latitude", "longitude"} <= set(df.columns):
            dims = [*extra, "latitude", "longitude"]
            n_lat, n_lon = df["latitude"].nunique(), df["longitude"].nunique()
            n_points = len(df[["latitude", "longitude"]].drop_duplicates())
            if n_lat * n_lon > SPARSE_GRID_CELLS and n_points < SPARSE_GRID_FILL * n_lat * n_lon:
                raise UsageError(
                    f"the {n_points:,} latitude/longitude pairs fill only "
                    f"{n_points / (n_lat * n_lon):.1%} of a {n_lat:,} x {n_lon:,} grid: they "
                    "look like scattered points; add a station id column (station_id, "
                    "station or site) to read them as stations"
                )
        elif extra:
            # An index or area-mean series: time (and leads, members) only.
            print(
                f"note: no station or latitude/longitude columns; reading one series along "
                f"{', '.join(extra)}. Pass --point LAT,LON[,ID] if it is one site.",
                file=sys.stderr,
            )
            dims = extra
        else:
            raise UsageError(
                "cannot tell the layout (if the header is not the first line, pass "
                "--skip-rows): the CSV needs a station column "
                "(station_id/station/site), latitude+longitude columns, a time column, "
                "or --index COLUMN"
            )
    station_dim = "station_id" if "station_id" in dims else None
    # A row with no station, time, level or lat/lon has nowhere to go: pandas would put
    # it on another row's label. Merged cells and footer (Total) rows leave these.
    keyless = df[dims].isna().any(axis=1)
    if keyless.any():
        empty = [d for d in dims if df[d].isna().any()]
        first = df.loc[keyless].iloc[0]
        sample = ", ".join(f"{c}={first[c]}" for c in df.columns[:6])
        if not drop_incomplete:
            raise UsageError(
                f"{int(keyless.sum()):,} rows have no {' or '.join(empty)} (first: {sample}): "
                "fill merged cells, or pass --drop-incomplete to leave such rows "
                "(footers, totals) out"
            )
        print(
            f"note: left out {int(keyless.sum()):,} rows with no {' or '.join(empty)}",
            file=sys.stderr,
        )
        df = df.loc[~keyless]
        if df.empty:
            raise UsageError("every row lacks a station, time or location; nothing to read")
    for col, limit in (("latitude", 90), ("longitude", 360)):
        if col not in df.columns:
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            sample = df.loc[pd.to_numeric(df[col], errors="coerce").isna(), col].iloc[0]
            raise UsageError(f"{col} values must be numbers in degrees; got {sample!r}")
        lowest = -90 if col == "latitude" else -180
        outside = df.loc[(df[col] < lowest) | (df[col] > limit), col]
        if len(outside):
            raise UsageError(
                f"{col} {outside.iloc[0]} is outside {lowest}..{limit}; are latitude and "
                "longitude swapped?"
            )
    if station_dim:
        df[station_dim] = df[station_dim].astype(str)
        if "longitude" in df.columns:
            df["longitude"] = df["longitude"].where(df["longitude"] <= 180, df["longitude"] - 360)
        located = {"latitude", "longitude"} <= set(df.columns)
        nowhere = df[["latitude", "longitude"]].isna().any(axis=1) if located else []
        if located and nowhere.any():
            names = sorted(df.loc[nowhere, station_dim].unique())
            raise UsageError(f"stations with no latitude/longitude: {', '.join(names[:10])}")

    dup = df.duplicated(subset=dims)
    if dup.any():
        row = df.loc[dup].iloc[0]
        key = ", ".join(f"{d}={row[d]}" for d in dims)
        labels = [
            c for c in df.columns if c not in dims and not pd.api.types.is_numeric_dtype(df[c])
        ]
        hint = f"; if {labels[0]} names the rows, pass --index {labels[0]}" if labels else ""
        raise UsageError(
            f"{int(dup.sum())} duplicate rows for the same {', '.join(dims)} (first: {key}){hint}"
        )

    # Other columns: station locations and per-station labels are station coords, a
    # constant time/step is a scalar coord, numbers are variables, other text is dropped.
    for col in (c for c in df.columns if c not in dims and c not in ("time", "step")):
        if not (pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col])):
            continue
        cells = df[col].dropna().astype(str)
        if cells.empty or not cells.str.match(r"\d{4}-\d{2}-\d{2}").all():
            continue
        parsed = pd.to_datetime(df[col], format="ISO8601", errors="coerce", utc=True)
        if parsed[df[col].notna()].notna().all():
            # An onset date or a forecast's init_time. Offsets become UTC; "[UTC]" or a
            # zone in the header says which clock other times of day are on; dates
            # alone stay as they are.
            zone = col_units.pop(col, None)
            wall = parsed.dt.tz_localize(None)
            given = wall.dropna()
            dated = (given == given.dt.normalize()).all()
            if zone and re.search(r"(?i)(dd|mm|yy|hh)", zone):
                zone = None
            if zone and not dated and not cells.str.contains(TZ_SUFFIX).any():
                wall = (
                    wall.dt.tz_localize(_parse_timezone(zone))
                    .dt.tz_convert("UTC")
                    .dt.tz_localize(None)
                )
            df[col] = wall
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
        elif (
            col in ("time", "step", "number", "level")
            or pd.api.types.is_datetime64_any_dtype(df[col])
        ) and df[col].nunique(dropna=False) == 1:
            scalar_coords[col] = df[col].iloc[0]
        elif station_dim and per_station and numeric and SITE_COLUMN.search(col):
            station_coords.append(col)
        elif (
            station_dim
            and per_station
            and len(dims) > 1
            and pd.api.types.is_datetime64_any_dtype(df[col])
        ):
            station_coords.append(col)
        elif numeric or pd.api.types.is_datetime64_any_dtype(df[col]):
            data_vars.append(col)
        elif col in col_units and units_convertible(col_units[col], col_units[col]):
            # A column under real units ("rain [mm]", not "Name (official)") is data;
            # its text cells must not make it vanish as a label.
            cells = df[col].mask(df[col].isin(NA_TEXT)).dropna()
            words = cells[pd.to_numeric(cells, errors="coerce").isna()]
            raise UsageError(
                f"{col} [{col_units[col]}] has text cells such as {words.iloc[0]!r}; map codes "
                "with --replace TEXT=NUMBER or --na-value TEXT (thousands separators such "
                "as 1,234 are not read)"
            )
        elif per_station:
            station_coords.append(col)
        else:
            dropped.append(col)
    if station_dim and not {"latitude", "longitude"} <= set(station_coords):
        raise UsageError(
            "station CSVs need latitude and longitude columns "
            "(or --point LAT,LON[,ID] for a single site)"
        )
    if dropped:
        print(f"note: dropped non-numeric columns: {', '.join(dropped)}", file=sys.stderr)
    if not data_vars:
        message = "no numeric data columns left after the coordinate columns"
        # Text columns, dropped or kept as station labels, with a sample cell.
        text = []
        for c in [*dropped, *station_coords]:
            cells = df[c].mask(df[c].isin(NA_TEXT)).dropna()
            words = cells[pd.to_numeric(cells, errors="coerce").isna()]
            if not pd.api.types.is_numeric_dtype(df[c]) and len(words):
                text.append(f"{c} ({words.iloc[0]!r})")
        if text:
            message += (
                f"; text in {', '.join(text)}: map codes with --replace TEXT=NUMBER "
                "or --na-value TEXT"
            )
        raise UsageError(message)

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

    unreadable = [
        f"{v} [{col_units[v]}]"
        for v in data_vars
        if v in col_units and not units_convertible(col_units[v], col_units[v])
    ]
    if unreadable:
        raise UsageError(
            f"units not understood: {', '.join(unreadable)}; give CF / UDUNITS units "
            "with --units NAME=UNITS (mm, mm day-1, degC, m s-1, %, 1)"
        )
    missing_units = [
        v
        for v in data_vars
        if v not in col_units and not pd.api.types.is_datetime64_any_dtype(df[v])
    ]
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
    "-i", "--input", type=Dataset("any"), help="Zarr to export; -o names the .csv or .xlsx."
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
    "--na-value",
    action="append",
    help="Missing-value code, e.g. -999: read treats it as missing, export writes it.",
)
@weather_skill.argument(
    "--date-format", help="strftime format of the time column, e.g. %%d/%%m/%%Y."
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
@weather_skill.argument("--sheet", help="Excel sheet name. Default: the first sheet, or Sheet1.")
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
@weather_skill.argument(
    "--drop-incomplete",
    action="store_true",
    help="Read: leave out rows with no station, time or lat/lon (footers, totals).",
)
@weather_skill.argument(
    "--timezone",
    help="Zone of times without an offset, e.g. Africa/Nairobi or +03:00 (default UTC).",
)
@weather_skill.argument(
    "--pivot",
    help="Export: NAME=VALUE; one column per entry of dim NAME, cells from variable VALUE.",
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
    pivot,
    timezone,
    drop_incomplete,
    **kwargs,
):
    """Export a weather-skills Zarr to a CSV table, or read a CSV or Excel sheet into a Zarr."""
    if (ds is None) == (csv is None):
        raise UsageError("pass exactly one of -i/--input (Zarr to export) or --csv (CSV to read)")
    if ds is not None:
        if output.suffix.lower() not in (".csv", *EXCEL_SUFFIXES):
            raise UsageError(
                f"export writes CSV or Excel; -o should end in .csv or .xlsx (got {output})"
            )
        read_only = (
            units,
            point,
            index,
            names,
            skip_rows,
            replace,
            drop_column,
            unpivot,
            drop_incomplete,
        )
        if any(read_only):
            raise UsageError("reading options (--units, --names, --unpivot, ...) are for --csv")
        _export(
            ds, output, variable, dropna, max_rows, na_value, date_format, sheet, pivot, timezone
        )
        return None
    if output.suffix.lower() in (".csv", *EXCEL_SUFFIXES):
        raise UsageError("reading a CSV writes a Zarr; -o should not end in .csv or .xlsx")
    if pivot:
        raise UsageError("--pivot is for export (-i); reading a wide table takes --unpivot")
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
        timezone,
        drop_incomplete,
    )
    missing = [v for v in variable or [] if v not in out.data_vars]
    if missing:
        raise UsageError(
            f"--variable not in CSV: {', '.join(missing)} (have {', '.join(out.data_vars)})"
        )
    return out[variable] if variable else out


if __name__ == "__main__":
    csv_io()

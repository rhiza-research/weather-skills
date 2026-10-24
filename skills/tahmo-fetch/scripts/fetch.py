# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
#   "tahmo",
# ]
#
# [tool.uv.sources]
# tahmo = { git = "https://github.com/rhiza-research/tahmo-api", rev = "8ed3adc22b5b7c53d08753e45676e9d4a0a52ab8" }
# ///
"""Fetch daily TAHMO weather-station observations as a weather-skills station Zarr."""
# Uses the TAHMO Python SDK. Credentials: TAHMO_API_USERNAME and TAHMO_API_PASSWORD.
#
# List stations in a region from the deployment API (no ``-o``)::
#
#     tahmo-fetch --list-stations --bbox N/W/S/E
#
# Then fetch chosen station IDs (and/or every TA station in a bbox).

import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import udunits_error
from weather_skills_core.standard_utils import is_transient, require_env
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.6"

DEFAULT_WORKERS = 8
_TAHMO_PREFIX = "TA"

VAR_MAP = {
    "pr": "precip",
    "te": "temperature",
    "rh": "humidity",
    "ap": "pressure",
}
DAILY_AGG = {
    "precip": "sum",
    "temperature": "mean",
    "humidity": "mean",
    "pressure": "mean",
    "tmax": "max",
    "tmin": "min",
}
# Daily extremes taken from the 5-minute temperature records, under the same
# complete-day rule as the other variables.
DERIVED = {"tmax": "temperature", "tmin": "temperature"}
EXTREMES = {
    "tmax": ("time: maximum", "daily maximum air temperature"),
    "tmin": ("time: minimum", "daily minimum air temperature"),
}
# TAHMO records every variable at a 5-minute interval, stamped at the interval
# end. A daily value is kept only when all 288 stamps of that UTC day passed the
# quality filter; any gap makes the day NaN. This applies to the means as well
# as the precip sum: a gap removes part of the diurnal cycle (e.g. the warm
# afternoon), so a partial-day mean is a biased estimate, not an observation.
SAMPLES_PER_DAY = 288
# (standard_name, units). Always CF/pint strings — TAHMO getVariables() uses
# "-" for dimensionless (relative humidity) and that fails pint.quantify().
CF_META = {
    "precip": ("lwe_precipitation_rate", "mm day-1"),
    "temperature": ("air_temperature", "degree_Celsius"),
    "humidity": ("relative_humidity", "1"),
    "pressure": ("air_pressure", "kPa"),
    "tmax": ("air_temperature", "degree_Celsius"),
    "tmin": ("air_temperature", "degree_Celsius"),
}
# TAHMO API leftovers if CF_META has no override.
_TAHMO_UNITS = {
    "-": "1",
    "degrees Celsius": "degree_Celsius",
    "degree Celsius": "degree_Celsius",
}


def _pint_units(raw) -> str | None:
    """Map a TAHMO/API units string to a pint-parseable CF spelling, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    return _TAHMO_UNITS.get(text, text)


def _coord_str(row, *keys) -> str:
    for key in keys:
        if key not in row.index:
            continue
        val = row[key]
        if val is None or val != val:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _station_name(row) -> str:
    return _coord_str(row, "location_name", "name")


def _ta_stations(stations):
    out = stations.dropna(subset=["location_latitude", "location_longitude"])
    return out[out["code"].astype(str).str.startswith(_TAHMO_PREFIX)].copy()


def _filter_bbox(stations, bbox):
    north, west, south, east = bbox
    lat = stations["location_latitude"]
    lon = stations["location_longitude"]
    lat_in = (lat >= south) & (lat <= north)
    if west <= east:
        lon_in = (lon >= west) & (lon <= east)
    else:
        lon_in = (lon >= west) | (lon <= east)
    return stations.loc[lat_in & lon_in].copy()


def _print_station_list(stations) -> None:
    ordered = stations.sort_values("code")
    writer = csv.writer(sys.stdout, dialect="excel-tab", lineterminator="\n")
    writer.writerow(["station_id", "name", "latitude", "longitude", "country"])
    for _, row in ordered.iterrows():
        writer.writerow(
            [
                str(row["code"]),
                _station_name(row),
                f"{float(row['location_latitude']):.5f}",
                f"{float(row['location_longitude']):.5f}",
                _coord_str(row, "location_countrycode"),
            ]
        )
    print(f"{len(ordered)} stations", file=sys.stderr)


def _select_stations(stations, station_ids, bbox):
    """Return deployment rows for explicit ``--station`` IDs, else TA stations in ``--bbox``."""
    if station_ids:
        wanted = [s.strip().upper() for s in station_ids]
        by_code = stations.set_index(stations["code"].astype(str).str.upper())
        missing = [s for s in wanted if s not in by_code.index]
        if missing:
            raise UsageError("unknown TAHMO station id(s): " + ", ".join(missing))
        return by_code.loc[wanted].reset_index(drop=True)

    if bbox is None:
        raise UsageError("pass --station ID (repeatable) and/or --bbox N/W/S/E.")

    selected = _filter_bbox(_ta_stations(stations), bbox)
    if selected.empty:
        north, west, south, east = bbox
        raise DataError(f"no TAHMO stations in --bbox {north:g}/{west:g}/{south:g}/{east:g}.")
    return selected


def _fetch_raw(api, station_id: str, start: str, end: str):
    """Call getRawData once, retrying a single transient error after a short backoff."""
    try:
        return api.getRawData(
            station=station_id, startDate=start, endDate=end, dataset="controlled"
        )
    except Exception as exc:
        if not is_transient(exc):
            raise
        print(f"{station_id}: transient error ({exc}); retrying once", file=sys.stderr)
        time.sleep(2.0)
        return api.getRawData(
            station=station_id, startDate=start, endDate=end, dataset="controlled"
        )


def _station_frame(api, station_id: str, start: str, end: str, day_start_hour: int = 0):
    """Return a daily DataFrame for one station (NaN for incomplete days), or None.

    Days ``start`` .. ``end`` inclusive, each starting at ``day_start_hour`` UTC.
    TAHMO stamps mark the end of each 5-minute interval, so day D holds the
    stamps in (D HH:00, D+1 HH:00]: the HH:00 stamp closes the previous day.
    """
    import pandas as pd

    offset = pd.Timedelta(hours=day_start_hour)
    first = pd.Timestamp(start) + offset
    last = pd.Timestamp(end) + pd.Timedelta(days=1) + offset
    try:
        # The API end bound is an inclusive date (00:00), so round up to the
        # date that includes the HH:00 stamp closing ``end``.
        raw = _fetch_raw(api, station_id, start, last.ceil("D").date().isoformat())
    except Exception as exc:  # noqa: BLE001
        print(f"{station_id}: DROPPED, fetch failed ({exc})", file=sys.stderr)
        return None
    if raw is None or len(raw) == 0:
        return None

    raw["time"] = pd.to_datetime(raw["time"], format="mixed", utc=True).dt.tz_convert(None)
    keep_vars = set(VAR_MAP.keys())
    raw = raw[raw["variable"].isin(keep_vars) & (raw["time"] > first) & (raw["time"] <= last)]
    if "quality" in raw.columns:
        raw = raw[raw["quality"] <= 2]
    if raw.empty:
        return None

    raw = raw.sort_values(["time", "variable", "quality"])
    raw = raw.drop_duplicates(["time", "variable"], keep="first")
    wide = raw.pivot(index="time", columns="variable", values="value")
    wide = wide.rename(columns=VAR_MAP)
    for name, source in DERIVED.items():
        if source in wide.columns:
            wide[name] = wide[source]

    agg_spec = {c: DAILY_AGG[c] for c in wide.columns if c in DAILY_AGG}
    if not agg_spec:
        return None
    # Re-stamp each record at its interval start, so the HH:00 stamp falls in
    # the day it closes, and shift by the day-start hour so each day bins as
    # one calendar day labelled by the date it starts on.
    wide.index = wide.index - pd.Timedelta(minutes=5) - offset
    days = wide[list(agg_spec)].resample("D")
    daily = days.agg(agg_spec)
    # Plain sum gives 0 mm for an empty day and an undercount for a partial
    # one; a mean over a partial day is biased. Keep only complete days.
    daily = daily.where(days.count() >= SAMPLES_PER_DAY)
    daily = daily.reindex(pd.date_range(start, end, freq="D", name="time"))
    daily["station_id"] = station_id
    return daily


def _ensure_setup(state):
    """Authenticate and load the deployment catalogue + variable metadata, at most once."""
    import pandas as pd

    if "api" not in state:
        username, password = require_env(
            "TAHMO_API_USERNAME",
            "TAHMO_API_PASSWORD",
            message="TAHMO_API_USERNAME and TAHMO_API_PASSWORD must be set.",
        )
        try:
            from TAHMO import apiWrapper
        except ImportError as exc:
            raise UsageError(
                f"could not import TAHMO ({exc}). Install via "
                f"'pip install git+https://github.com/rhiza-research/tahmo-api'."
            ) from None
        api_local = apiWrapper()
        api_local.setCredentials(username, password)
        stations_raw = api_local.getStations()
        stations_local = pd.json_normalize(list(stations_raw.values()), sep="_")
        state["api"] = api_local
        state["stations"] = stations_local
        state["var_meta"] = api_local.getVariables()
    return state["api"], state["stations"], state["var_meta"]


@weather_skill(
    name="tahmo-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--bbox",
    help=(
        "Select every TA station in this box. With --list-stations, the region "
        "to search on the TAHMO deployment API. Ignored for selection when "
        "--station IDs are given."
    ),
)
@weather_skill.argument(
    "--station",
    action="append",
    help="TAHMO station id (repeatable), e.g. TA00025. Discover ids with --list-stations.",
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=(
        f"Max concurrent per-station fetch threads (default {DEFAULT_WORKERS}). "
        "Lower this if TAHMO returns 429/throttling errors."
    ),
)
@weather_skill.argument(
    "--day-start-hour",
    type=int,
    default=0,
    help=(
        "UTC hour each daily value starts at (0-23, default 0). Day D covers "
        "D HH:00 to D+1 HH:00 UTC and is labelled D; e.g. 6 for KMSA's 06 UTC rainfall day."
    ),
)
@weather_skill.argument(
    "--list-stations",
    action="store_true",
    default=False,
    probe=True,
    help=(
        "Print TA stations in --bbox from the TAHMO deployment API as TSV on "
        "stdout (station_id, name, latitude, longitude, country) and exit. No -o."
    ),
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(
    start_time,
    end_time,
    workers,
    bbox,
    station,
    day_start_hour,
    list_stations,
    probe_latest,
    **kwargs,
):
    """Fetch daily TAHMO weather-station observations as a weather-skills station Zarr."""
    import pandas as pd
    import xarray as xr

    if not 0 <= day_start_hour <= 23:
        raise UsageError(f"--day-start-hour must be 0-23, got {day_start_hour}.")

    if list_stations and bbox is None:
        raise UsageError("--list-stations requires --bbox N/W/S/E.")
    if not list_stations and probe_latest is None and not station and bbox is None:
        raise UsageError("pass --station ID (repeatable) and/or --bbox N/W/S/E.")

    api, stations, var_meta = _ensure_setup({})

    if list_stations:
        _print_station_list(_filter_bbox(_ta_stations(stations), bbox))
        return

    if probe_latest is not None:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        end = now.date()
        start = end - timedelta(days=14)
        # Day D ends at D+1 HH:00 UTC, so the newest day that can be complete
        # is the one that ended last; before HH:00 that is two days back.
        newest = (now - timedelta(hours=day_start_hour)).date() - timedelta(days=1)
        if station or bbox is not None:
            selected = _select_stations(stations, station, bbox)
        else:
            selected = _ta_stations(stations).head(3)
        # Stop at the first station with that day complete instead of
        # downloading every station in the box.
        latest = None
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = [
            pool.submit(
                _station_frame,
                api,
                row["code"],
                start.isoformat(),
                end.isoformat(),
                day_start_hour,
            )
            for _, row in selected.iterrows()
        ]
        for fut in as_completed(futures):
            daily = fut.result()
            if daily is None:
                continue
            observed = daily.drop(columns="station_id").dropna(how="all")
            if observed.empty:
                continue
            day = observed.index.max().date()
            latest = day if latest is None else max(latest, day)
            if latest >= newest:
                break
        pool.shutdown(wait=False, cancel_futures=True)
        if latest is None:
            raise DataError("TAHMO probe found no recent complete days")
        print(latest.isoformat())
        return

    selected = _select_stations(stations, station, bbox)
    start = start_time.isoformat()
    end = end_time.isoformat()
    print(
        f"Fetching {len(selected)} TAHMO station(s) {start} → {end}, "
        f"days starting {day_start_hour:02d}:00 UTC",
        file=sys.stderr,
    )

    def _fetch_one(row):
        sid = row["code"]
        daily = _station_frame(api, sid, start, end, day_start_hour)
        if daily is None:
            return None
        return daily, {
            "station_id": sid,
            "latitude": float(row["location_latitude"]),
            "longitude": float(row["location_longitude"]),
            "country": _coord_str(row, "location_countrycode"),
            "name": _station_name(row),
        }

    frames = []
    meta_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, row): row["code"] for _, row in selected.iterrows()}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"{sid}: DROPPED, worker error ({exc})", file=sys.stderr)
                continue
            if result is None:
                continue
            daily, meta_row = result
            frames.append(daily)
            meta_rows.append(meta_row)
            complete = int(daily.drop(columns="station_id").notna().any(axis=1).sum())
            print(f"{sid}: {complete}/{len(daily)} days with a complete record", file=sys.stderr)

    if not frames:
        raise DataError("no data returned for any station.")

    df = pd.concat(frames).reset_index()
    meta = pd.DataFrame(meta_rows).drop_duplicates("station_id").set_index("station_id")
    df = df.set_index(["time", "station_id"])

    ds = xr.Dataset.from_dataframe(df)
    ids = ds["station_id"].values
    ds = ds.assign_coords(
        latitude=("station_id", meta.loc[ids, "latitude"].values),
        longitude=("station_id", meta.loc[ids, "longitude"].values),
        country=("station_id", meta.loc[ids, "country"].values),
        name=("station_id", meta.loc[ids, "name"].values),
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(
        standard_name="time",
        axis="T",
        comment=f"day D covers D {day_start_hour:02d}:00 to D+1 {day_start_hour:02d}:00 UTC",
    )
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="TAHMO station identifier")
    ds["country"].attrs.update(long_name="ISO 3166-1 alpha-2 country code")
    ds["name"].attrs.update(long_name="station name")
    short_code_for = {v: k for k, v in VAR_MAP.items()}
    for canonical in ds.data_vars:
        short = short_code_for.get(canonical)
        api_meta = var_meta.get(short, {}) if short else {}
        std_name, units_override = CF_META.get(canonical, (None, None))
        attrs = {"coordinates": "latitude longitude"}
        if std_name:
            attrs["standard_name"] = std_name
        units = _pint_units(units_override) or _pint_units(api_meta.get("units"))
        if not units:
            raise DataError(
                f"variable {canonical!r} has no pint-parseable units from TAHMO metadata."
            )
        exc = udunits_error(units)
        if exc is not None:
            raise DataError(
                f"units {units!r} for variable {canonical!r} are not udunits-valid "
                f"({exc}); refusing to write a non-CF store."
            )
        attrs["units"] = units
        description = api_meta.get("description")
        if description:
            attrs["long_name"] = description
        if canonical in EXTREMES:
            attrs["cell_methods"], attrs["long_name"] = EXTREMES[canonical]
        ds[canonical].attrs.update(attrs)
    ds.attrs.update(
        featureType="timeSeries",
        Conventions="CF-1.13",
        weather_skills_source="tahmo",
    )
    # A station is complete when it reported every requested day; multi-day
    # totals are only meaningful for those (missing days are never filled).
    n_days = (end_time - start_time).days + 1
    for name in sorted(ds.data_vars):
        complete = int((ds[name].notnull().sum("time") == n_days).sum())
        print(
            f"{name}: {complete} of {ds.sizes['station_id']} stations complete "
            f"(all {n_days} days {start} → {end})",
            file=sys.stderr,
        )
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

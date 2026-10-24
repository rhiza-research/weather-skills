# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "netcdf4",
#   "pandas",
#   "requests",
#   "zarr>=3",
# ]
# ///
"""Fetch WindBorne forecasts, soundings, conditions, climatology, analyses or degree days as a weather-skills Zarr."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, require_env
from weather_skills_core.units import convert_dataarray, stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_HOST = "https://api.windbornesystems.com"
_WM6 = f"{_HOST}/forecasts/v1/wm-6"

# 15 days of 3-hourly output from forecast zero.
_MAX_LEAD_HOURS = 360

# Parallel gridded requests: each is one (field, level, lead) zipped Zarr.
_WORKERS = 8

# The flags each --product reads; any other flag given exits 2.
_PRODUCT_FLAGS = {
    "grid": {"bbox", "date", "run", "variable", "level", "ensemble", "max_lead"},
    "point": {"point", "date", "run", "variable", "level", "distribution", "max_lead"},
    "sounding": {"point", "date", "run", "lead"},
    "conditions": {"point", "period"},
    "clim": {"point", "start_time", "end_time", "rolling_days"},
    "analysis": {"bbox", "start_time", "end_time", "variable", "level"},
    "degree-days": {"date", "run"},
}

# Short `-v` tokens for the most-used WeatherMesh-6 fields (mirroring the
# cfgrib names other fetchers use), mapped onto WindBorne names. Any other
# WindBorne name is accepted as-is. The bool marks pressure-level fields.
VARIABLES: dict[str, tuple[str, bool]] = {
    "tp": ("total_precipitation_3h", False),
    "t2m": ("temperature_2m", False),
    "d2m": ("dewpoint_2m", False),
    "msl": ("pressure_msl", False),
    "u10": ("wind_u_10m", False),
    "v10": ("wind_v_10m", False),
    "u100": ("wind_u_100m", False),
    "v100": ("wind_v_100m", False),
    "ws10": ("wind_speed_10m", False),
    "ws100": ("wind_speed_100m", False),
    "tmax": ("max_temperature_2m_3h", False),
    "tmin": ("min_temperature_2m_3h", False),
    "sp": ("surface_pressure", False),
    "tcwv": ("total_column_water_vapour", False),
    "cape": ("cape", False),
    "tcc": ("total_cloud_cover", False),
    "ssrd": ("solar_radiation_downwards_3h", False),
    "skt": ("skin_temperature", False),
    "t": ("temperature", True),
    "z": ("geopotential", True),
    "q": ("specific_humidity", True),
    "u": ("wind_u", True),
    "v": ("wind_v", True),
}
# WindBorne pressure-level names a point request may pass through as-is.
_UPPER = frozenset(
    {"temperature", "geopotential", "specific_humidity", "wind_u", "wind_v", "wind_speed"}
)

# The point endpoint's default fields, renamed to the short names above.
# `precipitation` is the same 3 h amount as total_precipitation_3h.
_POINT_DEFAULT_NAMES = {
    "temperature_2m": "t2m",
    "dewpoint_2m": "d2m",
    "precipitation": "tp",
    "pressure_msl": "msl",
    "wind_u_10m": "u10",
    "wind_v_10m": "v10",
    "wind_u_100m": "u100",
    "wind_v_100m": "v100",
    "wind_speed_10m": "ws10",
    "wind_speed_100m": "ws100",
}

# Sounding fields -> (output name, units, long name).
_SOUNDING_FIELDS = {
    "temperature_c": ("t", "degree_Celsius", "Air temperature"),
    "dewpoint_c": ("td", "degree_Celsius", "Dew point temperature"),
    "specific_humidity_kg_kg": ("q", "kg kg-1", "Specific humidity"),
    "wind_u_ms": ("u", "m s-1", "Eastward wind"),
    "wind_v_ms": ("v", "m s-1", "Northward wind"),
    "altitude_m": ("gh", "m", "Geopotential height"),
}

# Climatology response fields -> (short name, units). Values come in display units.
_CLIM_FIELDS = {
    "temperature_2m": ("t2m", "degree_Celsius"),
    "dewpoint_2m": ("d2m", "degree_Celsius"),
    "precipitation": ("tp", "mm"),
    "pressure_msl": ("msl", "hPa"),
    "wind_u_10m": ("u10", "m s-1"),
    "wind_v_10m": ("v10", "m s-1"),
    "wind_speed_10m": ("ws10", "m s-1"),
    "wind_u_100m": ("u100", "m s-1"),
    "wind_v_100m": ("v100", "m s-1"),
    "wind_speed_100m": ("ws100", "m s-1"),
}

# ECMWF analysis `-v` tokens -> (WindBorne name, pressure-level?).
_ANALYSIS_VARIABLES: dict[str, tuple[str, bool]] = {
    "tp": ("total_precipitation_1h", False),
    "t2m": ("temperature_2m", False),
    "d2m": ("dewpoint_2m", False),
    "msl": ("pressure_msl", False),
    "u10": ("wind_u_10m", False),
    "v10": ("wind_v_10m", False),
    "u100": ("wind_u_100m", False),
    "v100": ("wind_v_100m", False),
    "tcc": ("total_cloud_cover", False),
    "t": ("temperature", True),
    "z": ("geopotential", True),
    "q": ("specific_humidity", True),
    "u": ("wind_u", True),
    "v": ("wind_v", True),
}
_ANALYSIS_LEVELS = (10, 20, 30, 50, 70, 100, 150, 200, 250, 300)
_ANALYSIS_LEVELS += (400, 500, 600, 700, 800, 850, 900, 925, 950, 1000)

_KELVIN = frozenset(
    {
        "temperature_2m",
        "dewpoint_2m",
        "max_temperature_2m_3h",
        "min_temperature_2m_3h",
        "skin_temperature",
        "temperature",
    }
)

_VERTICAL_ATTRS = {"units": "hPa", "standard_name": "air_pressure", "positive": "down", "axis": "Z"}


def _get(session, url, params=None, *, binary=False):
    """GET with retries on 429/5xx. Raises DataError with the API's message otherwise."""
    import requests

    for attempt in range(4):
        try:
            resp = session.get(url, params=params, timeout=300)
        except requests.RequestException as exc:
            if attempt == 3:
                raise DataError(f"WindBorne request failed ({exc}).") from None
            time.sleep(2**attempt)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == 3:
                raise DataError(f"WindBorne API returned {resp.status_code}: {resp.text[:200]}")
            time.sleep(2**attempt)
            continue
        if resp.status_code == 401:
            raise UsageError(
                "WindBorne rejected the credentials (401). Check WB_CLIENT_ID and WB_API_KEY."
            )
        if resp.status_code == 403:
            raise DataError(f"WindBorne refused the request (403): {resp.text[:300]}")
        if resp.status_code != 200:
            raise DataError(f"WindBorne API returned {resp.status_code}: {resp.text[:300]}")
        return resp.content if binary else resp.json()
    raise AssertionError("unreachable")


def _parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def _parse_points(point):
    """--point LAT,LON[,ID] values -> (lats, lons, ids)."""
    rows = [p.split(",") for p in point]
    if any(len(r) not in (2, 3) for r in rows):
        raise UsageError("--point expects LAT,LON[,ID]")
    try:
        lats = [float(r[0]) for r in rows]
        lons = [float(r[1]) for r in rows]
    except ValueError:
        raise UsageError("point lat/lon must be numeric") from None
    ids = [r[2] if len(r) == 3 else f"P{i}" for i, r in enumerate(rows)]
    return lats, lons, ids


def _parse_max_lead(max_lead):
    """--max-lead as whole hours: bare numbers are hours, anything else a timedelta (48h, 2D)."""
    import pandas as pd

    text = str(_MAX_LEAD_HOURS if max_lead is None else max_lead).strip()
    try:
        hours = pd.Timedelta(f"{text}h" if text.isdigit() else text) / pd.Timedelta(1, "h")
    except ValueError:
        raise UsageError(f"--max-lead {text!r} is not a lead (48, 48h, 2D).") from None
    if hours % 3 or not 0 <= hours <= _MAX_LEAD_HOURS:
        raise UsageError(f"--max-lead must be a multiple of 3 h in 0..{_MAX_LEAD_HOURS} h.")
    return int(hours)


def _pick_init(session, available, date, run, *, archive):
    """The init to fetch: an explicit date+run, the latest run on a date, or the latest overall.

    With ``archive``, a date missing from the live list is looked up in the archive list.
    """
    if date is None and run is not None:
        raise UsageError("--run needs --date.")
    if date is None:
        return max(available)
    if run is not None:
        return dt.datetime.combine(date, dt.time(run))
    same_day = [t for t in available if t.date() == date]
    if not same_day and archive:
        # Older runs come from the archive list, paged backwards from page_end.
        page_end = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0))
        listed = _get(
            session,
            f"{_WM6}/initialization_times/archive",
            {"page_end": f"{page_end:%Y-%m-%dT%H:00:00Z}"},
        )
        same_day = [
            _parse_time(t)
            for t in listed.get("archived_initialization_times", [])
            if _parse_time(t).date() == date
        ]
    if not same_day:
        where = "in the live or archived run lists" if archive else "among the live runs"
        raise DataError(f"no WeatherMesh-6 run on {date} {where}.")
    return max(same_day)


@weather_skill(
    name="windborne-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--product",
    choices=("grid", "point", "sounding", "conditions", "clim", "analysis", "degree-days"),
    default="grid",
    help=(
        "What to fetch: grid (WeatherMesh-6 gridded forecast, default), point (point "
        "forecast), sounding (vertical profiles), conditions (plain-language summaries), "
        "clim (ERA5 1991-2020 hourly normals), analysis (ECMWF IFS analysis), "
        "degree-days (US population-weighted HDD/CDD)."
    ),
)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--point",
    action="append",
    help="point, sounding, conditions, clim: LAT,LON[,ID] (repeatable), e.g. --point -1.29,36.82,nairobi.",
)
@weather_skill.argument(
    "--date",
    help="Init date. Default: the latest run. grid also serves archived runs back to mid-2026.",
)
@weather_skill.argument(
    "--run",
    type=int,
    choices=range(24),
    metavar="HOUR",
    help="Init hour UTC (0-23). Default: the latest available run on --date.",
)
@weather_skill.argument(
    "--start-time", help="clim (required), analysis: first date. analysis default: the latest."
)
@weather_skill.argument("--end-time", help="clim (required), analysis: last date.")
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help=(
        "grid, point, analysis: field to retrieve (repeatable). Short names: tp, t2m, "
        "d2m, msl, u10, v10, u100, v100, ws10, ws100, tmax, tmin, sp, tcwv, cape, tcc, "
        "ssrd, skt; pressure-level t, z, q, u, v (need --level). grid and point also "
        "take any WindBorne variable name. Default: tp (grid), a bundle (point), t2m (analysis)."
    ),
)
@weather_skill.argument(
    "--level",
    type=int,
    action="append",
    help="Pressure level in hPa for pressure-level fields (repeatable), 10-1000.",
)
@weather_skill.argument(
    "--ensemble",
    choices=("mean", "std", "members"),
    help=(
        "grid: mean (default) and std of the 128-member ensemble, or all 128 members "
        "(only some fields: tp, t2m, d2m, msl, u10, v10, u100, v100, tcc, z at 500 hPa)."
    ),
)
@weather_skill.argument(
    "--distribution",
    action="store_true",
    help=(
        "point: also write each -v field's <name>_std and, where served, percentiles "
        "(<name>_p01 … <name>_p99) and tp exceedance probabilities (tp_gt_0p25mm … "
        "tp_gt_50mm). Needs -v."
    ),
)
@weather_skill.argument(
    "--max-lead",
    help=(
        "grid, point: last lead, hours (48) or a timedelta (48h, 2D); a multiple of "
        "3 h up to 360 h. Default 360."
    ),
)
@weather_skill.argument(
    "--lead",
    type=int,
    action="append",
    help="sounding: lead hour from forecast zero, a multiple of 3 up to 360 (repeatable). Default 0.",
)
@weather_skill.argument(
    "--period",
    choices=("daily", "day_night", "hourly"),
    help="conditions: daily (16 days, default), day_night (day and night halves), or hourly.",
)
@weather_skill.argument(
    "--rolling-days",
    type=int,
    help="clim: smooth the normals with a centred rolling mean over this many days. Default 0.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest init (or analysis) date, YYYY-MM-DD, on stdout and exit; "
        "stderr lists what is available. clim prints `none`. Downloads no fields."
    ),
)
def fetch(
    product,
    bbox,
    point,
    date,
    run,
    start_time,
    end_time,
    variable,
    level,
    ensemble,
    distribution,
    max_lead,
    lead,
    period,
    rolling_days,
    probe_latest,
    **kwargs,
):
    """Fetch WindBorne forecasts, soundings, conditions, climatology, analyses or degree days as a weather-skills Zarr."""
    import requests

    given = {
        "bbox": bbox,
        "point": point,
        "date": date,
        "run": run,
        "start_time": start_time,
        "end_time": end_time,
        "variable": variable,
        "level": level,
        "ensemble": ensemble,
        "distribution": distribution or None,
        "max_lead": max_lead,
        "lead": lead,
        "period": period,
        "rolling_days": rolling_days,
    }
    extra = [k for k, v in given.items() if v is not None and k not in _PRODUCT_FLAGS[product]]
    if extra:
        flags = ", ".join("--" + k.replace("_", "-") for k in extra)
        raise UsageError(f"--product {product} does not take {flags}.")

    if product == "clim" and probe_latest is not None:
        # The normals are static: there is no latest date, and no credentials are needed.
        print("none")
        return

    client_id, api_key = require_env(
        "WB_CLIENT_ID",
        "WB_API_KEY",
        message="WB_CLIENT_ID and WB_API_KEY must be set (WindBorne client ID and API key).",
    )
    session = requests.Session()
    # A client ID + key pair authenticates as HTTP Basic; Bearer returns 401.
    session.auth = (client_id, api_key)

    if product == "analysis":
        return _analysis(session, bbox, start_time, end_time, variable, level, probe_latest)

    needs_point = product in ("point", "sounding", "conditions", "clim")
    if needs_point and not point and probe_latest is None:
        raise UsageError(f"--product {product} needs --point LAT,LON[,ID].")
    if product == "clim":
        if start_time is None or end_time is None:
            raise UsageError("--product clim needs --start-time and --end-time.")
        return _clim(session, point, start_time, end_time, rolling_days or 0)
    if product == "conditions" and probe_latest is None:
        # Always the latest run: the endpoint takes no init.
        return _conditions(session, point, period or "daily")

    available = [
        _parse_time(t) for t in _get(session, f"{_WM6}/initialization_times").get("available", [])
    ]
    if not available:
        raise DataError("WindBorne lists no available WeatherMesh-6 runs.")
    if probe_latest is not None:
        latest = max(available)
        print(
            "Available runs: " + ", ".join(f"{t:%Y-%m-%d %HZ}" for t in sorted(available)),
            file=sys.stderr,
        )
        print(
            f"Latest run: {latest:%Y-%m-%d %H} UTC (--date {latest:%Y-%m-%d} --run {latest.hour})",
            file=sys.stderr,
        )
        print(latest.strftime("%Y-%m-%d"))
        return

    # Only the gridded endpoint serves archived runs; the others take the live list.
    init = _pick_init(session, available, date, run, archive=product == "grid")
    if product == "grid":
        return _grid(session, init, bbox, variable, level, ensemble or "mean", max_lead)
    if product == "point":
        return _point(session, init, point, variable, level, distribution, max_lead)
    if product == "sounding":
        return _sounding(session, init, point, lead)
    return _degree_days(session, init)


def _grid(session, init, bbox, variable, level, ensemble, max_lead):
    """WeatherMesh-6 gridded forecast: one zipped Zarr per (field, level, lead)."""
    import numpy as np
    import xarray as xr
    import zarr

    max_lead = _parse_max_lead(max_lead)

    # Resolve -v tokens to (output name, WindBorne name, levels or None).
    catalog = _get(session, f"{_WM6}/variables")
    surface_names = set(catalog["sfc_variables"]) | set(catalog["derived_sfc_variables"])
    upper_names = set(catalog["upper_variables"])
    wb_to_short = {wb: short for short, (wb, _) in VARIABLES.items()}
    fields = []
    for token in variable or ["tp"]:
        if token in VARIABLES:
            wb_name, upper = VARIABLES[token]
        elif token in surface_names or token in upper_names:
            wb_name, upper = token, token in upper_names
        else:
            raise UsageError(
                f"unknown variable {token!r}.\nShort names: {', '.join(VARIABLES)}\n"
                f"WindBorne surface: {', '.join(sorted(surface_names))}\n"
                f"WindBorne pressure-level: {', '.join(sorted(upper_names))}"
            )
        short = wb_to_short.get(wb_name, wb_name)
        if upper:
            if not level:
                raise UsageError(f"{token} is a pressure-level field; pass --level (hPa).")
            bad = [lev for lev in level if lev not in catalog["levels"]]
            if bad:
                raise UsageError(
                    f"level(s) {bad} not served; available: {', '.join(map(str, catalog['levels']))}."
                )
        if any(f[1] == wb_name for f in fields):
            continue
        fields.append((short, wb_name, sorted(level) if upper else None))
    if level and not any(f[2] for f in fields):
        raise UsageError("--level is only for pressure-level fields (t, z, q, u, v, ...).")
    # `_3h` fields cover the 3 h ending at their lead, so lead 0 holds nothing
    # inside the run (see the relabelling below).
    interval = [f[0] for f in fields if f[1].endswith("_3h")]
    if interval and max_lead == 0:
        raise UsageError(
            f"{', '.join(interval)} has no value at lead 0: it covers the 3 h ending at "
            "each lead. Pass --max-lead 3 or more, or leave it out of -v."
        )

    leads = list(range(0, max_lead + 1, 3))
    # Request flag and the group the array sits in, per --ensemble.
    flags, group_prefix = {
        "mean": ({}, ""),
        "std": ({"include_distribution": "true"}, "ensemble_std/"),
        "members": ({"include_members": "true"}, "members/"),
    }[ensemble]
    print(
        f"Fetching WindBorne WM-6 run {init:%Y-%m-%d %HZ} {ensemble} for "
        f"{', '.join(f[0] for f in fields)}, leads 0-{max_lead} h, bbox={list(bbox) if bbox else 'full key region'}",
        file=sys.stderr,
    )

    def fetch_one(job):
        """Download one (field, level, lead) zipped Zarr; return its bbox-clipped array and metadata."""
        wb_name, lev, lead = job
        params = {
            "variable": wb_name,
            "initialization_time": f"{init:%Y-%m-%dT%H:00:00Z}",
            "forecast_hour": lead,
            **flags,
        }
        if lev is not None:
            params["level"] = lev
        content = _get(session, f"{_WM6}/gridded", params, binary=True)
        path = tmpdir / f"{wb_name}_{lev}_{lead}.zarr.zip"
        path.write_bytes(content)
        group = zarr.open_group(zarr.storage.ZipStore(path, mode="r"), mode="r")
        array_name = wb_name if lev is None else f"{wb_name}_{lev}"
        key = group_prefix + array_name
        if key not in group:
            raise DataError(
                f"{wb_name}{'' if lev is None else f' at {lev} hPa'} has no {ensemble} output "
                f"(run /forecasts/v1/wm-6/variables for each field's distribution flavours)."
            )
        lat = group["latitude"][:]
        lon = group["longitude"][:]
        north, west, south, east = bbox or (np.inf, -np.inf, -np.inf, np.inf)
        lat_sel = (lat <= north) & (lat >= south)
        lon_sel = (lon >= west) & (lon <= east)
        if not lat_sel.any() or not lon_sel.any():
            raise UsageError(
                f"--bbox {list(bbox)} does not overlap the served grid "
                f"({lat.min()}..{lat.max()}N, {lon.min()}..{lon.max()}E), the region this key may read."
            )
        # Boxes are contiguous on a regular grid, so read only that window.
        rows, cols = np.flatnonzero(lat_sel), np.flatnonzero(lon_sel)
        window = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
        array = group[key]
        values = array[(slice(None),) * (array.ndim - 2) + window]
        path.unlink()
        return {
            "values": values,
            "lat": lat[window[0]],
            "lon": lon[window[1]],
            "units": group[key].attrs.get("units") or group[array_name].attrs.get("units"),
            "forecast_zero": group.attrs["forecast_zero"],
        }

    variables = {}
    forecast_zero = None
    with (
        tempfile.TemporaryDirectory(prefix="windborne-fetch-") as tmp,
        ThreadPoolExecutor(_WORKERS) as pool,
    ):
        tmpdir = Path(tmp)
        for short, wb_name, levels in fields:
            print(f"Retrieving {wb_name} ({len(leads)} leads)...", file=sys.stderr)
            per_level = []
            for lev in levels or [None]:
                parts = list(pool.map(fetch_one, [(wb_name, lev, lead) for lead in leads]))
                per_level.append(np.stack([p["values"] for p in parts]))
            first = parts[0]
            forecast_zero = forecast_zero or first["forecast_zero"]
            dims = ["step"]
            data = per_level[0] if levels is None else np.stack(per_level, axis=1)
            if levels is not None:
                dims.append("vertical")
            if ensemble == "members":
                dims.append("number")
            dims += ["latitude", "longitude"]
            coords = {"latitude": first["lat"], "longitude": first["lon"]}
            if levels is not None:
                coords["vertical"] = (
                    "vertical",
                    np.array(levels, dtype="float64"),
                    _VERTICAL_ATTRS,
                )
            if ensemble == "members":
                coords["number"] = np.arange(data.shape[dims.index("number")])
            da = xr.DataArray(data, dims=dims, coords=coords, attrs={"units": first["units"]})
            da.attrs["windborne_name"] = wb_name
            variables[short] = da

    ds = xr.Dataset(variables)
    ds = ds.assign_coords(
        step=("step", np.array(leads, dtype="timedelta64[h]").astype("timedelta64[ns]"))
    )
    # Leads count from forecast_zero, which for off-synoptic inits is earlier
    # than the initialization (publish) time.
    ds = ds.assign_coords(time=np.datetime64(_parse_time(forecast_zero), "ns"))
    ds = ds.sortby("latitude")

    if "tp" in ds.data_vars:
        # total_precipitation_3h is the amount over 3 h: a mm day-1 rate.
        ds["tp"] = ds["tp"] * 8.0
        ds["tp"].attrs.update(
            units="mm day-1",
            long_name="Total precipitation rate",
            windborne_name="total_precipitation_3h",
        )
    if interval:
        # `_3h` fields (tp, tmax, tmin, ssrd, ...) at lead h cover (h-3, h]:
        # label them at the interval start, like the other forecast fetchers.
        # Lead 0 covers the 3 h before forecast zero, so it is dropped. Every
        # other field is instantaneous at forecast zero + step and keeps its
        # lead; the merged axis is NaN where a field has no value (not filled).
        shifted = ds[interval].isel(step=slice(1, None))
        shifted = shifted.assign_coords(step=shifted["step"] - np.timedelta64(3, "h"))
        rest = ds.drop_vars(interval)
        ds = xr.merge([rest, shifted], join="outer") if rest.data_vars else shifted

    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-wm6",
        windborne_model="wm-6",
        windborne_initialization_time=f"{init:%Y-%m-%dT%H:00:00Z}",
        windborne_ensemble=ensemble,
    )
    stamp_cf_attrs(ds)
    # A spread is a difference: keep it in kelvin and pascals rather than
    # shifting temperatures by 273.15 as a unit conversion would.
    if ensemble != "std":
        # tp is already a mm day-1 rate.
        ds = to_standard_units(ds, [name for name in ds.data_vars if name != "tp"])
        for name in ds.data_vars:
            if (
                ds[name].attrs.get("windborne_name") in _KELVIN
                and ds[name].attrs.get("units") == "K"
            ):
                converted, _ = convert_dataarray(ds[name], "degree_Celsius")
                converted.attrs["units"] = "degree_Celsius"
                ds[name] = converted
    return stamp_data_interval(ds, "3 hour")


def _point_units(wb_name: str) -> str | None:
    """Units the point endpoint returns (it converts to display units: °C, hPa), or None if unknown."""
    if "temperature" in wb_name or "dewpoint" in wb_name:
        return "degree_Celsius"
    if "pressure" in wb_name:
        return "hPa"
    if "precipitation" in wb_name or wb_name in ("snowfall_3h", "runoff_3h"):
        return "mm"
    if "wind" in wb_name:
        return "m s-1"
    if "geopotential" in wb_name:
        return "m2 s-2"
    if wb_name == "total_column_water_vapour":
        return "kg m-2"
    if "humidity" in wb_name:
        return "kg kg-1"
    if "cloud_cover" in wb_name or wb_name.startswith("soil_moisture"):
        return "1"
    if wb_name == "cape":
        return "J kg-1"
    if "radiation" in wb_name or "flux" in wb_name:
        return "J m-2"
    if "height" in wb_name:
        return "m"
    return None


def _point(session, init, point, variable, level, distribution, max_lead):
    """WeatherMesh-6 forecast interpolated to points, as a station Zarr."""
    import numpy as np
    import xarray as xr

    lats, lons, ids = _parse_points(point)
    max_lead = _parse_max_lead(max_lead)
    if distribution and not variable:
        raise UsageError("--distribution needs -v (the default bundle has no distribution).")

    base = {
        "coordinates": ";".join(f"{lat},{lon}" for lat, lon in zip(lats, lons, strict=True)),
        "max_forecast_hour": max_lead,
        "initialization_time": f"{init:%Y-%m-%dT%H:00:00Z}",
    }

    # One request for the default bundle, or one per (field, level) for -v.
    requests_to_make = []
    for token in variable or [None]:
        if token is None:
            requests_to_make.append((None, None))
            continue
        wb_name, upper = VARIABLES.get(token, (token, token in _UPPER))
        if upper:
            if not level:
                raise UsageError(f"{token} is a pressure-level field; pass --level (hPa).")
            requests_to_make += [(wb_name, lev) for lev in sorted(level)]
        else:
            requests_to_make.append((wb_name, None))
    if level and all(lev is None for _, lev in requests_to_make):
        raise UsageError("--level is only for pressure-level fields (t, z, q, u, v, ...).")
    # Precipitation (the default bundle's `precipitation`, tp) and `_3h` fields
    # cover the 3 h ending at their lead, so lead 0 holds nothing inside the run.
    interval = [
        "tp (in the default bundle)" if wb_name is None else wb_name
        for wb_name, _ in requests_to_make
        if wb_name is None or wb_name.endswith("_3h")
    ]
    if interval and max_lead == 0:
        raise UsageError(
            f"{', '.join(interval)} has no value at lead 0: it covers the 3 h ending at "
            "each lead. Pass --max-lead 3 or more, or leave it out of -v."
        )

    short_of = {wb: short for short, (wb, _) in VARIABLES.items()} | _POINT_DEFAULT_NAMES
    columns: dict[str, dict] = {}
    forecast_zero = init_text = None
    times = None
    for wb_name, lev in requests_to_make:
        params = dict(base)
        if wb_name is not None:
            params["variable"] = wb_name
        if lev is not None:
            params["level"] = lev
        if distribution:
            params["include_distribution"] = "true"
        body = _get(session, f"{_WM6}/point_forecast/interpolated", params)
        forecast_zero = body["forecast_zero"]
        init_text = body["initialization_time"]
        series = body["forecasts"]
        times = [rec["time"] for rec in series[0]]
        keys = [
            k for k in series[0][0] if k not in ("time", "latitude", "longitude", "distribution")
        ]
        for key in keys:
            stem = key if lev is None else key[: -len(f"_{lev}")]
            name = short_of.get(stem, stem)
            if lev is not None:
                name = f"{name}{lev}"
            columns[name] = {
                "values": [[rec.get(key) for rec in s] for s in series],
                "units": _point_units(stem),
                "windborne_name": key,
                "interval": stem == "precipitation" or stem.endswith("_3h"),
            }
            for stat in series[0][0].get("distribution", {}):
                if stat == "mean":
                    continue
                units = _point_units(stem)
                if stat.startswith("gt_"):
                    units = "1"
                elif stat == "std" and units == "degree_Celsius":
                    # A spread is a difference: kelvin, so no 273.15 offset on conversion.
                    units = "K"
                columns[f"{name}_{stat}"] = {
                    "values": [[rec["distribution"].get(stat) for rec in s] for s in series],
                    "units": units,
                    "windborne_name": f"{key} {stat}",
                    "interval": columns[name]["interval"],
                }

    if not times:
        raise DataError("WindBorne returned no forecast times.")
    zero = _parse_time(forecast_zero)
    steps = np.array([_parse_time(t) - zero for t in times], dtype="timedelta64[ns]")
    ds = xr.Dataset(
        {
            name: (
                ("station_id", "step"),
                np.array(col["values"], dtype="float64"),
                # A field whose units are not known gets no units attr.
                {"windborne_name": col["windborne_name"]}
                | ({"units": col["units"]} if col["units"] else {}),
            )
            for name, col in columns.items()
        },
        coords={
            "station_id": ids,
            "latitude": ("station_id", lats),
            "longitude": ("station_id", lons),
            "step": steps,
            "time": np.datetime64(zero, "ns"),
        },
    )

    # Precip amounts per 3 h (tp, its spread and percentiles) become mm day-1
    # rates; exceedance probabilities (tp_gt_*) stay probabilities.
    for name in list(ds.data_vars):
        if name == "tp" or (name.startswith("tp_") and not name.startswith("tp_gt_")):
            ds[name] = ds[name] * 8.0
            ds[name].attrs.update(units="mm day-1", windborne_name=columns[name]["windborne_name"])
    # Interval fields (tp and its distribution, `_3h` fields) at lead h cover
    # (h-3, h]: label them at the interval start, and drop lead 0 (the 3 h
    # before forecast zero). Instantaneous fields keep their lead; the merged
    # axis is NaN where a field has no value (not filled).
    interval = [name for name, col in columns.items() if col["interval"]]
    if interval:
        shifted = ds[interval].isel(step=slice(1, None))
        shifted = shifted.assign_coords(step=shifted["step"] - np.timedelta64(3, "h"))
        rest = ds.drop_vars(interval)
        ds = xr.merge([rest, shifted], join="outer") if rest.data_vars else shifted

    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-wm6",
        windborne_model="wm-6",
        windborne_initialization_time=init_text,
        windborne_ensemble="mean",
    )
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, "3 hour")


def _sounding(session, init, point, lead):
    """WeatherMesh-6 vertical profiles at points, one request per point and lead."""
    import numpy as np
    import xarray as xr

    lats, lons, ids = _parse_points(point)
    leads = sorted(set(lead or [0]))
    if any(h % 3 or not 0 <= h <= _MAX_LEAD_HOURS for h in leads):
        raise UsageError(f"--lead must be a multiple of 3 in 0..{_MAX_LEAD_HOURS}.")

    profiles = {}
    forecast_zero = None
    for lat, lon in zip(lats, lons, strict=True):
        for h in leads:
            body = _get(
                session,
                f"{_WM6}/point_forecast/interpolated_sounding",
                {
                    "coordinates": f"{lat},{lon}",
                    "initialization_time": f"{init:%Y-%m-%dT%H:00:00Z}",
                    "forecast_hour": h,
                },
            )
            forecast_zero = body["forecast_zero"]
            profiles[(lat, lon, h)] = {rec["pressure_hpa"]: rec for rec in body["data"]}
    levels = sorted({p for prof in profiles.values() for p in prof}, reverse=True)
    if not levels:
        raise DataError("WindBorne returned an empty sounding.")

    data_vars = {}
    for key, (name, units, long_name) in _SOUNDING_FIELDS.items():
        values = np.full((len(ids), len(leads), len(levels)), np.nan)
        for i, (lat, lon) in enumerate(zip(lats, lons, strict=True)):
            for j, h in enumerate(leads):
                prof = profiles[(lat, lon, h)]
                values[i, j] = [prof.get(p, {}).get(key, np.nan) for p in levels]
        data_vars[name] = (
            ("station_id", "step", "vertical"),
            values,
            {"units": units, "long_name": long_name},
        )
    ds = xr.Dataset(
        data_vars,
        coords={
            "station_id": ids,
            "latitude": ("station_id", lats),
            "longitude": ("station_id", lons),
            "step": np.array(leads, dtype="timedelta64[h]").astype("timedelta64[ns]"),
            "vertical": ("vertical", np.array(levels, dtype="float64"), _VERTICAL_ATTRS),
            "time": np.datetime64(_parse_time(forecast_zero), "ns"),
        },
    )
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-wm6",
        windborne_model="wm-6",
        windborne_initialization_time=f"{init:%Y-%m-%dT%H:00:00Z}",
    )
    stamp_cf_attrs(ds)
    return ds


def _conditions(session, point, period):
    """Plain-language summaries at points from the latest WeatherMesh-6 run, also printed."""
    import numpy as np
    import xarray as xr

    lats, lons, ids = _parse_points(point)
    body = _get(
        session,
        f"{_WM6}/point_forecast/conditions",
        {"coordinates": ";".join(f"{lat},{lon}" for lat, lon in zip(lats, lons, strict=True))},
    )
    forecasts = body["forecasts"]
    if len(forecasts) != len(ids):
        raise DataError(f"WindBorne returned {len(forecasts)} locations for {len(ids)} points.")

    # Periods are keyed by start time; locations can differ (day/night edges
    # follow local sunrise), so take the union and leave gaps empty.
    starts = sorted({p["start_time"] for f in forecasts for p in f[period]})
    text = np.full((len(ids), len(starts)), "", dtype=object)
    conditions = np.full((len(ids), len(starts)), "", dtype=object)
    ends = np.full((len(ids), len(starts)), np.datetime64("NaT", "ns"))
    for i, f in enumerate(forecasts):
        print(f"{ids[i]} ({f['timezone']}):", file=sys.stderr)
        for p in f[period]:
            j = starts.index(p["start_time"])
            text[i, j] = p["text"]
            conditions[i, j] = "; ".join(p["conditions"])
            ends[i, j] = np.datetime64(_parse_time(p["end_time"]), "ns")
            label = f" {p['period']}" if "period" in p else ""
            print(f"  {p['start_time']}{label}: {p['text']}", file=sys.stderr)

    ds = xr.Dataset(
        {
            "text": (("station_id", "time"), text.astype(str), {"long_name": "Forecast summary"}),
            "conditions": (
                ("station_id", "time"),
                conditions.astype(str),
                {"long_name": "Ranked condition labels, most notable first"},
            ),
            "end_time": (("station_id", "time"), ends, {"long_name": "Period end"}),
        },
        coords={
            "station_id": ids,
            "latitude": ("station_id", lats),
            "longitude": ("station_id", lons),
            "time": np.array([_parse_time(s) for s in starts], dtype="datetime64[ns]"),
            "timezone": ("station_id", [f["timezone"] for f in forecasts]),
        },
    )
    ds.attrs.update(
        weather_skills_source="windborne-wm6",
        windborne_model="wm-6",
        windborne_initialization_time=body["initialization_time"],
        windborne_forecast_zero=body["forecast_zero"],
        windborne_period=period,
    )
    return ds


def _clim(session, point, start_time, end_time, rolling_days):
    """ERA5 1991-2020 hourly normals at points, laid on the requested dates."""
    import numpy as np
    import xarray as xr

    lats, lons, ids = _parse_points(point)
    if rolling_days < 0:
        raise UsageError("--rolling-days must be 0 or more.")

    # Hourly normals for every hour of the inclusive date range, plus 00Z the
    # next day: its precipitation covers the range's last hour.
    first = np.datetime64(f"{start_time:%Y-%m-%d}T00:00", "ns")
    last = np.datetime64(f"{end_time:%Y-%m-%d}T23:00", "ns")
    params = {
        "coordinates": ";".join(f"{lat},{lon}" for lat, lon in zip(lats, lons, strict=True)),
        "start_time": f"{start_time:%Y-%m-%d}T00:00:00Z",
        "end_time": f"{end_time + dt.timedelta(days=1):%Y-%m-%d}T00:00:00Z",
    }
    if rolling_days:
        params["rolling_average_days"] = rolling_days
    body = _get(session, f"{_HOST}/forecasts/v1/climatology", params)
    series = body["climatologies"]
    if len(series) != len(ids) or not series[0]:
        raise DataError("WindBorne returned no climatology for these points and dates.")

    times = np.array([_parse_time(r["time"]) for r in series[0]], dtype="datetime64[ns]")
    data_vars = {}
    for key, (name, units) in _CLIM_FIELDS.items():
        values = np.array(
            [[np.nan if r.get(key) is None else r[key] for r in s] for s in series], dtype="float64"
        )
        if np.isnan(values).all():
            continue
        data_vars[name] = (("station_id", "time"), values, {"units": units, "windborne_name": key})
    ds = xr.Dataset(
        data_vars,
        coords={
            "station_id": ids,
            "latitude": ("station_id", lats),
            "longitude": ("station_id", lons),
            "time": times,
        },
    )

    # ERA5 hourly precipitation at hour t is the amount over (t-1h, t]: make it
    # a mm day-1 rate labelled at the interval start. The other fields are
    # instantaneous and keep their hour. Both are then cut to the requested
    # days; an hour a field lacks stays NaN (not filled).
    if "tp" in ds.data_vars:
        tp = ds["tp"] * 24.0
        tp.attrs.update(units="mm day-1", windborne_name="precipitation")
        tp = tp.assign_coords(time=tp["time"] - np.timedelta64(1, "h"))
        rest = ds.drop_vars("tp")
        ds = xr.merge([rest, tp], join="outer") if rest.data_vars else tp.to_dataset()
    ds = ds.sel(time=slice(first, last))

    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-climatology",
        climatology_normal=body.get("normal", "1991-2020"),
        climatology_rolling_days=rolling_days,
    )
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, "1 hour")


def _analysis(session, bbox, start_time, end_time, variable, level, probe_latest):
    """ECMWF IFS deterministic analysis served by WindBorne, one NetCDF per field and time."""
    import numpy as np
    import xarray as xr

    api = f"{_HOST}/forecasts/v1/ecmwf_det_anl/analysis"
    available = sorted(_parse_time(t) for t in _get(session, f"{api}/available_times")["available"])
    if not available:
        raise DataError("WindBorne lists no ECMWF analysis times.")
    if probe_latest is not None:
        print(
            f"Analyses every 6 h from {available[0]:%Y-%m-%d %HZ} to {available[-1]:%Y-%m-%d %HZ}",
            file=sys.stderr,
        )
        print(available[-1].strftime("%Y-%m-%d"))
        return

    if start_time is None and end_time is not None:
        raise UsageError("--end-time needs --start-time.")
    if start_time is None:
        times = [available[-1]]
    else:
        last = end_time or start_time
        times = [t for t in available if start_time <= t.date() <= last]
        if not times:
            raise DataError(
                f"no analyses between {start_time} and {last}; served: "
                f"{available[0]:%Y-%m-%d} to {available[-1]:%Y-%m-%d}."
            )

    fields = []
    for token in variable or ["t2m"]:
        if token not in _ANALYSIS_VARIABLES:
            raise UsageError(
                f"unknown variable {token!r}. Available: {', '.join(_ANALYSIS_VARIABLES)}"
            )
        if any(f[0] == token for f in fields):
            continue
        wb_name, upper = _ANALYSIS_VARIABLES[token]
        if upper:
            if not level:
                raise UsageError(f"{token} is a pressure-level field; pass --level (hPa).")
            bad = [lev for lev in level if lev not in _ANALYSIS_LEVELS]
            if bad:
                raise UsageError(
                    f"level(s) {bad} not served; available: {', '.join(map(str, _ANALYSIS_LEVELS))}."
                )
        fields.append((token, wb_name, sorted(level) if upper else None))
    if level and not any(f[2] for f in fields):
        raise UsageError("--level is only for pressure-level fields (t, z, q, u, v).")

    print(
        f"Fetching ECMWF analysis via WindBorne: {len(times)} times, "
        f"{', '.join(f[0] for f in fields)}, bbox={list(bbox) if bbox else 'global'}",
        file=sys.stderr,
    )

    # One file at a time: netCDF4/HDF5 is not thread-safe, and parallel opens
    # corrupt its state for later reads and writes in the same process.
    with tempfile.TemporaryDirectory(prefix="windborne-analysis-") as tmp:
        variables = {}
        for short, wb_name, levels in fields:
            per_level = []
            for lev in levels or [None]:
                name = wb_name if lev is None else f"{wb_name}_{lev}"
                print(f"Retrieving {name} ({len(times)} times)...", file=sys.stderr)
                parts = []
                for when in times:
                    path = Path(tmp) / f"{name}_{when:%Y%m%d%H}.nc"
                    path.write_bytes(
                        _get(
                            session,
                            f"{api}/gridded",
                            {"variable": name, "time": f"{when:%Y-%m-%dT%H:00:00Z}"},
                            binary=True,
                        )
                    )
                    with xr.open_dataset(path) as ds:
                        # Longitudes come 0..180 then -180..0; sort before cutting the box.
                        da = ds[name].sortby("longitude")
                        if bbox is not None:
                            da = bbox_subset(
                                da.to_dataset(), list(bbox), lat_dim="latitude", lon_dim="longitude"
                            )[name]
                        parts.append(da.load())
                    path.unlink()
                da = xr.concat(parts, dim="time").assign_coords(
                    time=np.array(times, dtype="datetime64[ns]")
                )
                per_level.append(da.rename(short))
            if levels is None:
                da = per_level[0]
            else:
                da = xr.concat(per_level, dim="vertical").assign_coords(
                    vertical=("vertical", np.array(levels, dtype="float64"), _VERTICAL_ATTRS)
                )
                da = da.transpose("time", "vertical", "latitude", "longitude")
            da.attrs["windborne_name"] = wb_name
            variables[short] = da

    ds = xr.Dataset(variables).sortby("latitude")
    if "tp" in ds.data_vars:
        # total_precipitation_1h covers (T-1h, T] before each analysis time T: a
        # mm day-1 rate over that one hour, labelled at its start T-1h like the
        # other fetchers. The other five hours of each 6 h step are not served,
        # so it is not a 6-hourly mean. The other fields keep their analysis
        # times; the merged axis is NaN where a field has no value (not filled).
        units = str(ds["tp"].attrs.get("units") or "mm")
        tp, _ = convert_dataarray(ds["tp"], "mm") if units != "mm" else (ds["tp"], None)
        tp = tp * 24.0
        tp.attrs.update(units="mm day-1", windborne_name="total_precipitation_1h")
        tp = tp.assign_coords(time=tp["time"] - np.timedelta64(1, "h"))
        rest = ds.drop_vars("tp")
        ds = xr.merge([rest, tp], join="outer") if rest.data_vars else tp.to_dataset()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-ecmwf-analysis",
        source="ECMWF IFS Analysis (Deterministic) via WindBorne",
    )
    stamp_cf_attrs(ds)
    ds = to_standard_units(ds, [name for name in ds.data_vars if name != "tp"])
    for name in ds.data_vars:
        if ds[name].attrs.get("windborne_name") in _KELVIN and ds[name].attrs.get("units") == "K":
            converted, _ = convert_dataarray(ds[name], "degree_Celsius")
            converted.attrs["units"] = "degree_Celsius"
            ds[name] = converted
    # data_interval is per variable: the analysis fields are 6-hourly samples
    # (stamped when there is more than one), tp a one-hour rate.
    if len(times) > 1:
        ds = stamp_data_interval(ds, "6 hour")
    if "tp" in ds.data_vars:
        ds["tp"].attrs["data_interval"] = "1 hour"
    return ds


def _degree_days(session, init):
    """Population-weighted US heating and cooling degree days from one WeatherMesh-6 run."""
    import numpy as np
    import xarray as xr

    params = {"initialization_time": f"{init:%Y-%m-%dT%H:00:00Z}"}
    hdd = _get(session, f"{_HOST}/insights/v1/wm-6/hdds", params)
    cdd = _get(session, f"{_HOST}/insights/v1/wm-6/cdds", params)
    # `dates` can list a final day the tables leave out or hold only as nulls;
    # keep days with values.
    dates = [
        day
        for day in hdd["dates"]
        if any(v is not None for v in hdd["hdd"].get(day, {}).values())
        and any(v is not None for v in cdd["cdd"].get(day, {}).values())
    ]
    if not dates:
        raise DataError(f"WindBorne returned no degree days for the {init:%Y-%m-%d %HZ} run.")
    states = sorted(hdd["hdd"][dates[0]])
    data_vars = {}
    for name, body, long_name in (
        ("hdd", hdd, "Population-weighted heating degree days"),
        ("cdd", cdd, "Population-weighted cooling degree days"),
    ):
        table = body[name]
        values = np.array(
            [[table.get(day, {}).get(state, np.nan) for day in dates] for state in states],
            dtype="float64",
        )
        data_vars[name] = (
            ("region", "time"),
            values,
            {"units": "degree_Fahrenheit day", "long_name": long_name},
        )
    ds = xr.Dataset(
        data_vars,
        coords={
            "region": states,
            "time": np.array(dates, dtype="datetime64[D]").astype("datetime64[ns]"),
        },
    )
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-wm6",
        windborne_model="wm-6",
        windborne_initialization_time=hdd.get("metadata", {}).get(
            "initialization_time", f"{init:%Y-%m-%dT%H:00:00Z}"
        ),
    )
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, "1 day")


if __name__ == "__main__":
    fetch()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "requests",
# ]
# ///
"""Fetch WindBorne WeatherMesh-6 vertical soundings at chosen locations as a weather-skills Zarr."""

from __future__ import annotations

import datetime as dt
import sys
import time

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import require_env

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API = "https://api.windbornesystems.com/forecasts/v1/wm-6"

_MAX_LEAD_HOURS = 360

# Sounding fields -> (output name, units, long name).
_FIELDS = {
    "temperature_c": ("t", "degree_Celsius", "Air temperature"),
    "dewpoint_c": ("td", "degree_Celsius", "Dew point temperature"),
    "specific_humidity_kg_kg": ("q", "kg kg-1", "Specific humidity"),
    "wind_u_ms": ("u", "m s-1", "Eastward wind"),
    "wind_v_ms": ("v", "m s-1", "Northward wind"),
    "altitude_m": ("gh", "m", "Geopotential height"),
}


def _get(session, url, params=None):
    """GET JSON with retries on 429/5xx. Raises DataError with the API's message otherwise."""
    import requests

    for attempt in range(4):
        try:
            resp = session.get(url, params=params, timeout=120)
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
        if resp.status_code != 200:
            raise DataError(f"WindBorne API returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise AssertionError("unreachable")


def _parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


@weather_skill(
    name="windborne-sounding-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--point",
    action="append",
    required=True,
    help="LAT,LON[,ID] (repeatable), e.g. --point -1.29,36.82,nairobi.",
)
@weather_skill.argument(
    "--date",
    help="Init date. Default: the latest run. Only live runs (about the last two days) are served.",
)
@weather_skill.argument(
    "--run",
    type=int,
    choices=range(24),
    metavar="HOUR",
    help="Init hour UTC (0-23). Default: the latest available run on --date.",
)
@weather_skill.argument(
    "--lead",
    type=int,
    action="append",
    help="Lead hour from forecast zero, a multiple of 3 up to 360 (repeatable). Default 0.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest init date (YYYY-MM-DD, usable as --date) on stdout and "
        "exit; stderr lists every available run. Does not download soundings."
    ),
)
def fetch(point, date, run, lead, probe_latest, **kwargs):
    """Fetch WindBorne WeatherMesh-6 vertical soundings at chosen locations as a weather-skills Zarr."""
    import numpy as np
    import requests
    import xarray as xr

    client_id, api_key = require_env(
        "WB_CLIENT_ID",
        "WB_API_KEY",
        message="WB_CLIENT_ID and WB_API_KEY must be set (WindBorne client ID and API key).",
    )
    session = requests.Session()
    session.auth = (client_id, api_key)
    runs = [_parse_time(t) for t in _get(session, f"{_API}/initialization_times")["available"]]
    if probe_latest is not None:
        latest = max(runs)
        print(
            "Available runs: " + ", ".join(f"{t:%Y-%m-%d %HZ}" for t in sorted(runs)),
            file=sys.stderr,
        )
        print(
            f"Latest run: {latest:%Y-%m-%d %H} UTC (--date {latest:%Y-%m-%d} --run {latest.hour})",
            file=sys.stderr,
        )
        print(latest.strftime("%Y-%m-%d"))
        return

    rows = [p.split(",") for p in point]
    if any(len(r) not in (2, 3) for r in rows):
        raise UsageError("--point expects LAT,LON[,ID]")
    try:
        lats = [float(r[0]) for r in rows]
        lons = [float(r[1]) for r in rows]
    except ValueError:
        raise UsageError("point lat/lon must be numeric") from None
    ids = [r[2] if len(r) == 3 else f"P{i}" for i, r in enumerate(rows)]
    leads = sorted(set(lead or [0]))
    if any(h % 3 or not 0 <= h <= _MAX_LEAD_HOURS for h in leads):
        raise UsageError(f"--lead must be a multiple of 3 in 0..{_MAX_LEAD_HOURS}.")

    # The sounding endpoint needs an explicit init: default to the latest run.
    if date is None and run is not None:
        raise UsageError("--run needs --date.")
    if date is None:
        init = max(runs)
    elif run is None:
        same_day = [t for t in runs if t.date() == date]
        if not same_day:
            raise DataError(
                f"no WeatherMesh-6 run on {date} among the available runs. "
                "This endpoint serves only the live runs (about the last two days); "
                "for an older run use windborne-fetch."
            )
        init = max(same_day)
    else:
        init = dt.datetime.combine(date, dt.time(run))

    # One request per point and lead: the endpoint takes a single coordinate.
    profiles = {}
    forecast_zero = None
    for lat, lon in zip(lats, lons, strict=True):
        for h in leads:
            body = _get(
                session,
                f"{_API}/point_forecast/interpolated_sounding",
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
    for key, (name, units, long_name) in _FIELDS.items():
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
            "vertical": (
                "vertical",
                np.array(levels, dtype="float64"),
                {"units": "hPa", "standard_name": "air_pressure", "positive": "down", "axis": "Z"},
            ),
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


if __name__ == "__main__":
    fetch()

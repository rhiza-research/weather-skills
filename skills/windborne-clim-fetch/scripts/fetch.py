# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "requests",
# ]
# ///
"""Fetch WindBorne's ERA5 1991-2020 hourly climatology at chosen locations as a weather-skills station Zarr."""

from __future__ import annotations

import datetime as dt
import time

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import require_env
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API = "https://api.windbornesystems.com/forecasts/v1/climatology"

# Response fields -> (short name, units). Values come in display units.
_FIELDS = {
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


def _get(session, url, params=None):
    """GET JSON with retries on 429/5xx. Raises DataError with the API's message otherwise."""
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
        if resp.status_code != 200:
            raise DataError(f"WindBorne API returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise AssertionError("unreachable")


@weather_skill(
    name="windborne-clim-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--point",
    action="append",
    required=True,
    help="LAT,LON[,ID] (repeatable), e.g. --point -1.29,36.82,nairobi.",
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--rolling-days",
    type=int,
    default=0,
    help="Smooth the normals with a centred rolling mean over this many days. Default 0 (none).",
)
def fetch(point, start_time, end_time, rolling_days, **kwargs):
    """Fetch WindBorne's ERA5 1991-2020 hourly climatology at chosen locations as a weather-skills station Zarr."""
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

    rows = [p.split(",") for p in point]
    if any(len(r) not in (2, 3) for r in rows):
        raise UsageError("--point expects LAT,LON[,ID]")
    try:
        lats = [float(r[0]) for r in rows]
        lons = [float(r[1]) for r in rows]
    except ValueError:
        raise UsageError("point lat/lon must be numeric") from None
    ids = [r[2] if len(r) == 3 else f"P{i}" for i, r in enumerate(rows)]
    if rolling_days < 0:
        raise UsageError("--rolling-days must be 0 or more.")

    # Hourly normals for every hour of the inclusive date range.
    params = {
        "coordinates": ";".join(f"{lat},{lon}" for lat, lon in zip(lats, lons, strict=True)),
        "start_time": f"{start_time:%Y-%m-%d}T00:00:00Z",
        "end_time": f"{end_time:%Y-%m-%d}T23:00:00Z",
    }
    if rolling_days:
        params["rolling_average_days"] = rolling_days
    body = _get(session, _API, params)
    series = body["climatologies"]
    if len(series) != len(ids) or not series[0]:
        raise DataError("WindBorne returned no climatology for these points and dates.")

    times = np.array(
        [dt.datetime.fromisoformat(r["time"].replace("Z", "")) for r in series[0]],
        dtype="datetime64[ns]",
    )
    data_vars = {}
    for key, (name, units) in _FIELDS.items():
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
    # a mm day-1 rate labelled at the interval start.
    if "tp" in ds.data_vars:
        ds["tp"] = ds["tp"] * 24.0
        ds["tp"].attrs.update(units="mm day-1", windborne_name="precipitation")
        ds = ds.assign_coords(time=ds["time"] - np.timedelta64(1, "h"))

    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-climatology",
        climatology_normal=body.get("normal", "1991-2020"),
        climatology_rolling_days=rolling_days,
    )
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, "1 hour")


if __name__ == "__main__":
    fetch()

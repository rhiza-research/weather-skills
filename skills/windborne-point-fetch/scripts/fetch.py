# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "requests",
# ]
# ///
"""Fetch a WindBorne WeatherMesh-6 point forecast at chosen locations as a weather-skills station Zarr."""

from __future__ import annotations

import datetime as dt
import time

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import require_env
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API = "https://api.windbornesystems.com/forecasts/v1/wm-6"

_MAX_LEAD_HOURS = 360

# The point endpoint's default fields, renamed to the short names the gridded
# skill uses. `precipitation` is the same 3 h amount as total_precipitation_3h.
_DEFAULT_NAMES = {
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

# Short `-v` tokens, as in windborne-fetch. Any other WindBorne name is passed through.
VARIABLES: dict[str, str] = {
    "tp": "total_precipitation_3h",
    "t2m": "temperature_2m",
    "d2m": "dewpoint_2m",
    "msl": "pressure_msl",
    "u10": "wind_u_10m",
    "v10": "wind_v_10m",
    "u100": "wind_u_100m",
    "v100": "wind_v_100m",
    "ws10": "wind_speed_10m",
    "ws100": "wind_speed_100m",
    "tmax": "max_temperature_2m_3h",
    "tmin": "min_temperature_2m_3h",
    "sp": "surface_pressure",
    "tcwv": "total_column_water_vapour",
    "cape": "cape",
    "tcc": "total_cloud_cover",
    "ssrd": "solar_radiation_downwards_3h",
    "skt": "skin_temperature",
    "t": "temperature",
    "z": "geopotential",
    "q": "specific_humidity",
    "u": "wind_u",
    "v": "wind_v",
}
_UPPER = frozenset(
    {"temperature", "geopotential", "specific_humidity", "wind_u", "wind_v", "wind_speed"}
)


def _units_for(wb_name: str) -> str:
    """Units the point endpoint returns (it converts to display units: °C, hPa)."""
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
    return ""


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
    name="windborne-point-fetch",
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
    "--variable",
    "-v",
    action="append",
    help=(
        "Field to retrieve (repeatable). Default: t2m, d2m, tp, msl, u10, v10, u100, "
        "v100, ws10, ws100 in one request. Short names as in windborne-fetch, or any "
        "WindBorne variable name; pressure-level t, z, q, u, v need --level."
    ),
)
@weather_skill.argument(
    "--level",
    type=int,
    action="append",
    help="Pressure level in hPa for pressure-level fields (repeatable), 10-1000.",
)
@weather_skill.argument(
    "--distribution",
    action="store_true",
    help=(
        "Also write the ensemble distribution of each -v field: <name>_std and, "
        "where served, percentiles (<name>_p10 …) and tp exceedance probabilities "
        "(tp_gt_2p5mm …). Needs -v."
    ),
)
@weather_skill.argument(
    "--max-lead",
    type=int,
    default=_MAX_LEAD_HOURS,
    help="Last lead hour to fetch (multiple of 3, up to 360). Default 360.",
)
def fetch(point, date, run, variable, level, distribution, max_lead, **kwargs):
    """Fetch a WindBorne WeatherMesh-6 point forecast at chosen locations as a weather-skills station Zarr."""
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
    if max_lead % 3 or not 0 <= max_lead <= _MAX_LEAD_HOURS:
        raise UsageError(f"--max-lead must be a multiple of 3 in 0..{_MAX_LEAD_HOURS}.")
    if distribution and not variable:
        raise UsageError("--distribution needs -v (the default bundle has no distribution).")

    base = {
        "coordinates": ";".join(f"{lat},{lon}" for lat, lon in zip(lats, lons, strict=True)),
        "max_forecast_hour": max_lead,
    }
    if date is None and run is not None:
        raise UsageError("--run needs --date.")
    if date is not None:
        if run is None:
            runs = _get(session, f"{_API}/initialization_times")
            same_day = [_parse_time(t) for t in runs["available"] if _parse_time(t).date() == date]
            if not same_day:
                raise DataError(
                    f"no WeatherMesh-6 run on {date} among the available runs. "
                    "This endpoint serves only the live runs (about the last two days); "
                    "for an older run use windborne-fetch."
                )
            run = max(same_day).hour
        base["initialization_time"] = f"{date:%Y-%m-%d}T{run:02d}:00:00Z"

    # One request for the default bundle, or one per (field, level) for -v.
    requests_to_make = []
    for token in variable or [None]:
        if token is None:
            requests_to_make.append((None, None))
            continue
        wb_name = VARIABLES.get(token, token)
        if wb_name in _UPPER:
            if not level:
                raise UsageError(f"{token} is a pressure-level field; pass --level (hPa).")
            requests_to_make += [(wb_name, lev) for lev in sorted(level)]
        else:
            requests_to_make.append((wb_name, None))

    short_of = {wb: short for short, wb in VARIABLES.items()} | _DEFAULT_NAMES
    columns: dict[str, dict] = {}
    forecast_zero = init = None
    times = None
    for wb_name, lev in requests_to_make:
        params = dict(base)
        if wb_name is not None:
            params["variable"] = wb_name
        if lev is not None:
            params["level"] = lev
        if distribution:
            params["include_distribution"] = "true"
        body = _get(session, f"{_API}/point_forecast/interpolated", params)
        forecast_zero = body["forecast_zero"]
        init = body["initialization_time"]
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
                "units": _units_for(stem),
                "windborne_name": key,
            }
            for stat in series[0][0].get("distribution", {}):
                if stat == "mean":
                    continue
                units = _units_for(stem)
                if stat.startswith("gt_"):
                    units = "1"
                elif stat == "std" and units == "degree_Celsius":
                    # A spread is a difference: kelvin, so no 273.15 offset on conversion.
                    units = "K"
                columns[f"{name}_{stat}"] = {
                    "values": [[rec["distribution"].get(stat) for rec in s] for s in series],
                    "units": units,
                    "windborne_name": f"{key} {stat}",
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
                {"units": col["units"], "windborne_name": col["windborne_name"]},
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

    # Precip at lead h is the amount over (h-3, h]: make it a mm day-1 rate
    # labelled at the interval start, and drop lead 0 (before forecast zero).
    tp_names = [
        n for n in ds.data_vars if n == "tp" or (n.startswith("tp_") and not n.startswith("tp_gt_"))
    ]
    if tp_names:
        for name in tp_names:
            ds[name] = ds[name] * 8.0
            ds[name].attrs.update(units="mm day-1", windborne_name=columns[name]["windborne_name"])
        ds = ds.isel(step=slice(1, None))
        ds = ds.assign_coords(step=ds["step"] - np.timedelta64(3, "h"))

    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="windborne-wm6",
        windborne_model="wm-6",
        windborne_initialization_time=init,
    )
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, "3 hour")


if __name__ == "__main__":
    fetch()

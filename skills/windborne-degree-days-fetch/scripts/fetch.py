# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "requests",
# ]
# ///
"""Fetch WindBorne's WeatherMesh-6 population-weighted US degree-day forecast as a weather-skills Zarr."""

from __future__ import annotations

import datetime as dt
import time

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.standard_utils import require_env

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API = "https://api.windbornesystems.com"


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
    name="windborne-degree-days-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--date",
    help="Init date. Default: the latest run. Archived runs need --run too.",
)
@weather_skill.argument(
    "--run",
    type=int,
    choices=range(24),
    metavar="HOUR",
    help="Init hour UTC (0-23). Default: the latest available run on --date.",
)
def fetch(date, run, **kwargs):
    """Fetch WindBorne's WeatherMesh-6 population-weighted US degree-day forecast as a weather-skills Zarr."""
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

    if date is None and run is not None:
        raise UsageError("--run needs --date.")
    runs = [
        _parse_time(t)
        for t in _get(session, f"{_API}/forecasts/v1/wm-6/initialization_times")["available"]
    ]
    if date is None:
        init = max(runs)
    elif run is None:
        same_day = [t for t in runs if t.date() == date]
        if not same_day:
            raise DataError(
                f"no WeatherMesh-6 run on {date} among the available runs. "
                "For an archived run pass --run too."
            )
        init = max(same_day)
    else:
        init = dt.datetime.combine(date, dt.time(run))
    params = {"initialization_time": f"{init:%Y-%m-%dT%H:00:00Z}"}

    hdd = _get(session, f"{_API}/insights/v1/wm-6/hdds", params)
    cdd = _get(session, f"{_API}/insights/v1/wm-6/cdds", params)
    # `dates` can list a final day the tables do not hold yet; keep days with values.
    dates = [day for day in hdd["dates"] if day in hdd["hdd"] and day in cdd["cdd"]]
    if not dates:
        raise DataError(f"WindBorne returned no degree days for the {init:%Y-%m-%d %HZ} run.")
    states = sorted(hdd["hdd"][dates[0]])
    data_vars = {}
    for name, body, key, long_name in (
        ("hdd", hdd, "hdd", "Population-weighted heating degree days"),
        ("cdd", cdd, "cdd", "Population-weighted cooling degree days"),
    ):
        table = body[key]
        values = np.array(
            [[table.get(day, {}).get(state, np.nan) for day in dates] for state in states],
            dtype="float64",
        )
        data_vars[name] = (
            ("region", "time"),
            values,
            {"units": "degree_Fahrenheit day", "long_name": long_name, "data_interval": "1 day"},
        )
    ds = xr.Dataset(
        data_vars,
        coords={
            "region": states,
            "time": np.array(dates, dtype="datetime64[D]").astype("datetime64[ns]"),
        },
    )
    ds.attrs.update(
        weather_skills_source="windborne-wm6",
        windborne_model="wm-6",
        windborne_initialization_time=hdd.get("metadata", {}).get(
            "initialization_time", f"{init:%Y-%m-%dT%H:00:00Z}"
        ),
    )
    return ds


if __name__ == "__main__":
    fetch()

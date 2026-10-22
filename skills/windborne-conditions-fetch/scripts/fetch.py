# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "requests",
# ]
# ///
"""Fetch WindBorne's plain-language forecast conditions at chosen locations as a weather-skills Zarr."""

from __future__ import annotations

import datetime as dt
import sys
import time

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.standard_utils import require_env

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API = "https://api.windbornesystems.com/forecasts/v1/wm-6"


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
    name="windborne-conditions-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--point",
    action="append",
    required=True,
    help="LAT,LON[,ID] (repeatable), e.g. --point -1.29,36.82,nairobi.",
)
@weather_skill.argument(
    "--period",
    choices=("daily", "day_night", "hourly"),
    default="daily",
    help="Summary length: daily (16 days), day_night (day and night halves), or hourly. Default daily.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest init date (YYYY-MM-DD) on stdout and exit; stderr lists "
        "every available run. Conditions always come from the latest run."
    ),
)
def fetch(point, period, probe_latest, **kwargs):
    """Fetch WindBorne's plain-language forecast conditions at chosen locations as a weather-skills Zarr."""
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

    if probe_latest is not None:
        runs = [_parse_time(t) for t in _get(session, f"{_API}/initialization_times")["available"]]
        latest = max(runs)
        print(
            "Available runs: " + ", ".join(f"{t:%Y-%m-%d %HZ}" for t in sorted(runs)),
            file=sys.stderr,
        )
        print(f"Latest run: {latest:%Y-%m-%d %H} UTC", file=sys.stderr)
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

    body = _get(
        session,
        f"{_API}/point_forecast/conditions",
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


if __name__ == "__main__":
    fetch()

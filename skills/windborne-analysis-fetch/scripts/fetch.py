# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "requests",
#   "netcdf4",
# ]
# ///
"""Fetch the ECMWF IFS deterministic analysis served by WindBorne as a weather-skills Zarr."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import time
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, require_env
from weather_skills_core.units import convert_dataarray, stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API = "https://api.windbornesystems.com/forecasts/v1/ecmwf_det_anl"


# Short `-v` tokens -> (WindBorne name, pressure-level?).
VARIABLES: dict[str, tuple[str, bool]] = {
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
DEFAULT_VARIABLES = ["t2m"]
LEVELS = (
    10,
    20,
    30,
    50,
    70,
    100,
    150,
    200,
    250,
    300,
    400,
    500,
    600,
    700,
    800,
    850,
    900,
    925,
    950,
    1000,
)
_KELVIN = frozenset({"temperature_2m", "dewpoint_2m", "temperature"})


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
        if resp.status_code != 200:
            raise DataError(f"WindBorne API returned {resp.status_code}: {resp.text[:300]}")
        return resp.content if binary else resp.json()
    raise AssertionError("unreachable")


def _parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


@weather_skill(
    name="windborne-analysis-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--start-time",
    help="First analysis date. Default: the latest analysis. About the last 25 days are served.",
)
@weather_skill.argument("--end-time", help="Last analysis date. Default: --start-time.")
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help=(
        "Field (repeatable). tp, t2m, d2m, msl, u10, v10, u100, v100, tcc; "
        "pressure-level t, z, q, u, v (need --level). Default t2m."
    ),
)
@weather_skill.argument(
    "--level",
    type=int,
    action="append",
    help="Pressure level in hPa for pressure-level fields (repeatable), 10-1000.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help="Print the latest analysis date on stdout and exit; stderr gives the served range.",
)
def fetch(bbox, start_time, end_time, variable, level, probe_latest, **kwargs):
    """Fetch the ECMWF IFS deterministic analysis served by WindBorne as a weather-skills Zarr."""
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

    available = sorted(
        _parse_time(t) for t in _get(session, f"{_API}/analysis/available_times")["available"]
    )
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
    for token in variable or DEFAULT_VARIABLES:
        if token not in VARIABLES:
            raise UsageError(f"unknown variable {token!r}. Available: {', '.join(VARIABLES)}")
        if any(f[0] == token for f in fields):
            continue
        wb_name, upper = VARIABLES[token]
        if upper:
            if not level:
                raise UsageError(f"{token} is a pressure-level field; pass --level (hPa).")
            bad = [lev for lev in level if lev not in LEVELS]
            if bad:
                raise UsageError(
                    f"level(s) {bad} not served; available: {', '.join(map(str, LEVELS))}."
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
                            f"{_API}/analysis/gridded",
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
                    vertical=(
                        "vertical",
                        np.array(levels, dtype="float64"),
                        {
                            "units": "hPa",
                            "standard_name": "air_pressure",
                            "positive": "down",
                            "axis": "Z",
                        },
                    )
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


if __name__ == "__main__":
    fetch()

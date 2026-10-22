# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "requests",
#   "zarr>=3",
# ]
# ///
"""Fetch a WindBorne WeatherMesh-6 gridded forecast from the WindBorne API as a weather-skills Zarr."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import require_env
from weather_skills_core.units import convert_dataarray, stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API = "https://api.windbornesystems.com/forecasts/v1/wm-6"

# 15 days of 3-hourly output from forecast zero.
_MAX_LEAD_HOURS = 360

# Parallel gridded requests: each is one (field, level, lead) zipped Zarr.
_WORKERS = 8

# Short `-v` tokens for the most-used fields (mirroring the cfgrib names other
# fetchers use), mapped onto WindBorne names. Any other WindBorne name listed
# by /variables is accepted as-is. The bool marks pressure-level fields.
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
DEFAULT_VARIABLES = ["tp"]

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


def _get(session, url, params=None, *, binary=False):
    """GET with retries on 429/5xx. Raises DataError with the API's message otherwise."""
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
        if resp.status_code == 403:
            raise DataError(f"WindBorne refused the request (403): {resp.text[:300]}")
        if resp.status_code != 200:
            raise DataError(f"WindBorne API returned {resp.status_code}: {resp.text[:300]}")
        return resp.content if binary else resp.json()
    raise AssertionError("unreachable")


@weather_skill(
    name="windborne-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--date",
    help="Init date. Default: the latest run. Archived runs back to mid-2026 are served.",
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
        "Field to retrieve (repeatable). Short names: tp, t2m, d2m, msl, u10, v10, "
        "u100, v100, ws10, ws100, tmax, tmin, sp, tcwv, cape, tcc, ssrd, skt; "
        "pressure-level t, z, q, u, v (need --level). Any WindBorne variable name "
        "is also accepted. Default tp."
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
    default="mean",
    help=(
        "mean (default) and std of the 128-member ensemble, or all 128 members "
        "(only some fields: tp, t2m, d2m, msl, u10, v10, u100, v100, tcc, z at 500 hPa)."
    ),
)
@weather_skill.argument(
    "--max-lead",
    type=int,
    default=_MAX_LEAD_HOURS,
    help="Last lead hour to fetch (multiple of 3, up to 360). Default 360.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest init date (YYYY-MM-DD, usable as --date) on stdout and "
        "exit; stderr lists every available run. Does not download fields."
    ),
)
def fetch(bbox, date, run, variable, level, ensemble, max_lead, probe_latest, **kwargs):
    """Fetch a WindBorne WeatherMesh-6 gridded forecast from the WindBorne API as a weather-skills Zarr."""
    import numpy as np
    import requests
    import xarray as xr
    import zarr

    client_id, api_key = require_env(
        "WB_CLIENT_ID",
        "WB_API_KEY",
        message="WB_CLIENT_ID and WB_API_KEY must be set (WindBorne client ID and API key).",
    )
    session = requests.Session()
    # A client ID + key pair authenticates as HTTP Basic; Bearer returns 401.
    session.auth = (client_id, api_key)

    runs = _get(session, f"{_API}/initialization_times")
    available = [
        dt.datetime.fromisoformat(t).replace(tzinfo=None) for t in runs.get("available", [])
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

    # Pick the init: an explicit date+run, the latest run on a date, or the latest overall.
    if date is None and run is not None:
        raise UsageError("--run needs --date.")
    if date is None:
        init = max(available)
    elif run is None:
        same_day = [t for t in available if t.date() == date]
        if not same_day:
            # Older runs come from the archive list, paged backwards from page_end.
            page_end = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0))
            archive = _get(
                session,
                f"{_API}/initialization_times/archive",
                {"page_end": f"{page_end:%Y-%m-%dT%H:00:00Z}"},
            )
            same_day = [
                dt.datetime.fromisoformat(t).replace(tzinfo=None)
                for t in archive.get("archived_initialization_times", [])
            ]
            same_day = [t for t in same_day if t.date() == date]
        if not same_day:
            raise DataError(f"no WeatherMesh-6 run on {date} in the live or archived run lists.")
        init = max(same_day)
    else:
        init = dt.datetime.combine(date, dt.time(run))

    if max_lead % 3 or not 0 <= max_lead <= _MAX_LEAD_HOURS:
        raise UsageError(f"--max-lead must be a multiple of 3 in 0..{_MAX_LEAD_HOURS}.")

    # Resolve -v tokens to (output name, WindBorne name, level or None).
    catalog = _get(session, f"{_API}/variables")
    surface_names = set(catalog["sfc_variables"]) | set(catalog["derived_sfc_variables"])
    upper_names = set(catalog["upper_variables"])
    wb_to_short = {wb: short for short, (wb, _) in VARIABLES.items()}
    fields = []
    for token in variable or DEFAULT_VARIABLES:
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
        content = _get(session, f"{_API}/gridded", params, binary=True)
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
                    {
                        "units": "hPa",
                        "standard_name": "air_pressure",
                        "positive": "down",
                        "axis": "Z",
                    },
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
    forecast_zero = dt.datetime.fromisoformat(forecast_zero).replace(tzinfo=None)
    ds = ds.assign_coords(time=np.datetime64(forecast_zero, "ns"))
    ds = ds.sortby("latitude")

    if "tp" in ds.data_vars:
        # total_precipitation_3h at lead h is the amount over (h-3, h]. Turn it
        # into a mm day-1 rate labelled at the interval start, like the other
        # forecast fetchers; lead 0 covers the 3 h before forecast zero, so drop it.
        ds["tp"] = ds["tp"] * 8.0
        ds["tp"].attrs.update(
            units="mm day-1",
            long_name="Total precipitation rate",
            windborne_name="total_precipitation_3h",
        )
        ds = ds.isel(step=slice(1, None))
        ds = ds.assign_coords(step=ds["step"] - np.timedelta64(3, "h"))

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
    ds = stamp_data_interval(ds, "3 hour")
    return ds


if __name__ == "__main__":
    fetch()

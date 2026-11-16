# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "requests",
#   "xarray",
#   "netcdf4",
#   "numpy",
# ]
# ///
"""Fetch SMOS daily L3 surface soil moisture (CATDS, one overpass) over a bbox as a weather-skills Zarr."""

# CATDS (CNES/IFREMER) serves one global ~7 MB .tgz per day and overpass, on
# the 25 km EASE-2 grid (lat spacing varies, lon is regular). The reprocessed
# RE07 archive runs 2010-01-17..2021-05-24; the operational OPER files carry on
# from 2021-05-25. Open HTTPS, no login.

from __future__ import annotations

import datetime as dt
import re
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_BASE = "https://data.catds.fr/cpdc/Land_products/GRIDDED/L3SM"
_FIRST_DAY = dt.date(2010, 1, 17)
_LAST_RE07 = dt.date(2021, 5, 24)
# SMOS crosses the equator ascending at 06:00 and descending at 18:00 local time.
_PRODUCT = {"AM": "MIR_CLF31A", "PM": "MIR_CLF31D"}
_WORKERS = 4
_WARN_DAYS = 366


def _day_dir(day, overpass):
    archive = "RE07" if day <= _LAST_RE07 else "OPER"
    return f"{_BASE}/{archive}/{_PRODUCT[overpass]}/{day.year}/{day.timetuple().tm_yday:03d}/"


def _listing(session, url, pattern):
    resp = session.get(url, timeout=60)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return sorted(set(re.findall(pattern, resp.text)))


def _latest(session, overpass):
    top = f"{_BASE}/OPER/{_PRODUCT[overpass]}/"
    year = _listing(session, top, r'href="(\d{4})/?"')[-1]
    doy = _listing(session, f"{top}{year}/", r'href="(\d{3})/?"')[-1]
    return dt.date(int(year), 1, 1) + dt.timedelta(days=int(doy) - 1)


def _fetch_day(session, day, overpass, workdir):
    """The day's NetCDF path, or None when CATDS has no file for it."""
    url = _day_dir(day, overpass)
    names = _listing(session, url, r'href="(SM_[^"/]+\.tgz)"')
    if not names:
        return None
    resp = session.get(url + names[-1], timeout=300)
    resp.raise_for_status()
    tgz = Path(workdir) / names[-1]
    tgz.write_bytes(resp.content)
    with tarfile.open(tgz) as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith(".nc")), None)
        if member is None:
            raise DataError(f"{names[-1]} holds no NetCDF.")
        tar.extract(member, workdir, filter="data")
    tgz.unlink()
    return Path(workdir) / member.name


@weather_skill(name="smos-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--overpass",
    choices=tuple(_PRODUCT),
    default="AM",
    help="AM = 6am ascending, PM = 6pm descending. Default AM.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help="Print the last day CATDS has for --overpass (YYYY-MM-DD) and exit.",
)
def fetch(start_time, end_time, bbox, overpass, **kwargs):
    """Fetch SMOS daily L3 surface soil moisture (CATDS, one overpass) over a bbox as a weather-skills Zarr."""
    import numpy as np
    import requests
    import xarray as xr

    session = requests.Session()
    if kwargs.get("probe_latest") is not None:
        try:
            print(_latest(session, overpass).isoformat())
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"could not list CATDS ({exc}).") from None
        return None

    if start_time is None or end_time is None:
        raise UsageError("pass --start-time and --end-time (YYYY-MM-DD).")
    if end_time < start_time:
        raise UsageError("--end-time is before --start-time.")
    if start_time < _FIRST_DAY:
        raise UsageError(f"the SMOS L3 record starts {_FIRST_DAY.isoformat()}.")
    days = [start_time + dt.timedelta(days=i) for i in range((end_time - start_time).days + 1)]
    if len(days) > _WARN_DAYS:
        print(
            f"smos-fetch: warning: {len(days)} days is about {7 * len(days) // 1000} GB of "
            "global files; expect a long download.",
            file=sys.stderr,
        )
    print(f"smos-fetch: {_PRODUCT[overpass]} {start_time}..{end_time}: {len(days)} day(s)", file=sys.stderr)

    pieces, missing = [], []
    with tempfile.TemporaryDirectory(prefix="smos-fetch-") as tmp:

        def one(day):
            try:
                return day, _fetch_day(session, day, overpass, tmp)
            except requests.RequestException as exc:
                raise DataError(f"CATDS download failed for {day} ({exc}).") from None

        with ThreadPoolExecutor(_WORKERS) as pool:
            results = list(pool.map(one, days))
        for day, path in results:
            if path is None:
                missing.append(day)
                continue
            with xr.open_dataset(path) as raw:
                da = raw["Soil_Moisture"].rename(lat="latitude", lon="longitude").sortby("latitude")
                da = ensure_normalized_longitude(da.to_dataset(name="soil_moisture"))
                if bbox is not None:
                    da = bbox_subset(da, bbox, lat_dim="latitude", lon_dim="longitude")
                pieces.append(da.expand_dims(time=[np.datetime64(day.isoformat(), "ns")]).load())
    if missing:
        print(
            f"smos-fetch: no CATDS file for {len(missing)} day(s), skipped: "
            + ", ".join(d.isoformat() for d in missing[:10])
            + (" ..." if len(missing) > 10 else ""),
            file=sys.stderr,
        )
    if not pieces:
        raise DataError(f"CATDS has no {overpass} SMOS files for {start_time}..{end_time}.")

    ds = xr.concat(pieces, dim="time")
    ds["soil_moisture"] = ds["soil_moisture"].astype("float32")
    ds["soil_moisture"].attrs = {
        "units": "m3 m-3",
        "standard_name": "volume_fraction_of_condensed_water_in_soil",
        "long_name": f"SMOS surface soil moisture, {overpass} overpass",
        "comment": "Top ~5 cm. Native 25 km EASE-2 grid: latitude spacing varies.",
    }
    ds.attrs = {
        "Conventions": "CF-1.13",
        "title": f"SMOS L3 daily surface soil moisture, {overpass} overpass ({_PRODUCT[overpass]})",
        "weather_skills_source": f"catds:smos-l3sm:{_PRODUCT[overpass]}",
        "institution": "CATDS, operated for CNES by IFREMER",
        "source": f"{_BASE} (RE07 to {_LAST_RE07.isoformat()}, OPER after)",
        "references": "https://doi.org/10.5194/essd-9-293-2017",
        "acknowledgement": 'These data were obtained from the "Centre Aval de Traitement des '
        'Données SMOS" (CATDS), operated for the "Centre National d\'Etudes Spatiales" '
        "(CNES, France) by IFREMER (Brest, France)",
        "license": "Free use with the CATDS acknowledgement",
    }
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

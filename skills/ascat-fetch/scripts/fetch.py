# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "ecmwf-datastores-client==0.4.2",
#   "requests",
#   "xarray",
#   "netcdf4",
#   "numpy",
# ]
# ///
"""Fetch daily ASCAT surface soil moisture (C3S ACTIVE product) over a bbox from the Copernicus CDS."""

# The C3S "satellite-soil-moisture" ACTIVE product merges the scatterometers
# (ERS-1/2 AMI from 1991, Metop ASCAT-A/B/C from 2007) onto a 0.25 deg daily
# grid, as percent of saturation. The CDS cannot cut a box, so each day is a
# ~1 MB global file; the box is cut after download. One ECMWF account key
# works for the CDS and the ADS.

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# The CDS API base URL is a fixed, public endpoint, not a secret.
_CDS_URL = "https://cds.climate.copernicus.eu/api"
_CDS_HOST = "cds.climate.copernicus.eu"
_DATASET = "satellite-soil-moisture"
_FIRST_DAY = dt.date(1991, 8, 5)
_WARN_DAYS = 366

_POLL_SECONDS = 10
_POLL_MAX_SECONDS = 3600

# CDS netCDF name -> output name, long_name.
_VARS = {
    "sm": ("soil_moisture_saturation", "Surface soil moisture, percent of saturation"),
    "sm_uncertainty": (
        "soil_moisture_saturation_uncertainty",
        "Surface soil moisture uncertainty, percent of saturation",
    ),
}


def _catalogue_days():
    """{date: (version, type_of_record)} for every daily ACTIVE file the CDS lists, newest version first."""
    import requests

    base = f"{_CDS_URL}/catalogue/v1/collections/{_DATASET}"
    try:
        links = requests.get(base, timeout=30).json()["links"]
        url = next(link["href"] for link in links if link["rel"] == "constraints")
        constraints = requests.get(url, timeout=60).json()
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"could not read the CDS catalogue for {_DATASET} ({exc}).") from None

    days = {}
    for row in constraints:
        if "active" not in row.get("type_of_sensor", []) or "daily" not in row.get(
            "time_aggregation", []
        ):
            continue
        if "surface_soil_moisture_saturation" not in row.get("variable", []):
            continue
        for version in row["version"]:
            if version.startswith("deprecated"):
                continue
            for record in row["type_of_record"]:
                for y in row["year"]:
                    for m in row["month"]:
                        for d in row["day"]:
                            try:
                                day = dt.date(int(y), int(m), int(d))
                            except ValueError:
                                continue
                            # v202505_2 > v202505 > v202312: the newest version wins.
                            if day not in days or version > days[day][0]:
                                days[day] = (version, record)
    if not days:
        raise DataError(f"the CDS catalogue lists no daily ACTIVE files for {_DATASET}.")
    return days


def _key():
    """CDS_API_KEY, else the key in ~/.cdsapirc (a CDS or ADS one: the token is the same)."""
    key = os.environ.get("CDS_API_KEY")
    rc = Path("~/.cdsapirc").expanduser()
    if not key and rc.is_file():
        fields = dict(line.partition(":")[::2] for line in rc.read_text().splitlines())
        key = {k.strip(): v.strip() for k, v in fields.items()}.get("key")
    if not key:
        raise UsageError(
            "no CDS credentials: set CDS_API_KEY (your personal token from "
            f"https://{_CDS_HOST}/profile), or write a ~/.cdsapirc with `url: {_CDS_URL}` "
            "and `key: <token>`."
        )
    return key


@weather_skill(name="ascat-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--uncertainty",
    action="store_true",
    help="Also write soil_moisture_saturation_uncertainty.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help="Print the last day the CDS lists (YYYY-MM-DD) and exit. No key needed.",
)
def fetch(start_time, end_time, bbox, uncertainty, **kwargs):
    """Fetch daily ASCAT surface soil moisture (C3S ACTIVE product) over a bbox from the Copernicus CDS."""
    import numpy as np
    import requests
    import xarray as xr

    if kwargs.get("probe_latest") is not None:
        print(max(_catalogue_days()).isoformat())
        return None

    if start_time is None or end_time is None:
        raise UsageError("pass --start-time and --end-time (YYYY-MM-DD).")
    if end_time < start_time:
        raise UsageError("--end-time is before --start-time.")
    if start_time < _FIRST_DAY:
        raise UsageError(f"the ACTIVE soil moisture record starts {_FIRST_DAY.isoformat()}.")

    available = _catalogue_days()
    wanted = [start_time + dt.timedelta(days=i) for i in range((end_time - start_time).days + 1)]
    missing = [d for d in wanted if d not in available]
    if len(missing) == len(wanted):
        raise DataError(
            f"the CDS lists no ACTIVE soil moisture for {start_time}..{end_time}; the last day "
            f"is {max(available).isoformat()} (`--probe-latest`)."
        )
    if missing:
        print(
            f"ascat-fetch: {len(missing)} day(s) not on the CDS, skipped: "
            + ", ".join(d.isoformat() for d in missing[:10])
            + (" ..." if len(missing) > 10 else ""),
            file=sys.stderr,
        )
    if len(wanted) > _WARN_DAYS:
        print(
            f"ascat-fetch: warning: {len(wanted)} days is about {len(wanted)} MB of global "
            "files; expect a long download.",
            file=sys.stderr,
        )

    # One request per (version, record, month).
    groups: dict[tuple, list[dt.date]] = {}
    for day in wanted:
        if day in available:
            groups.setdefault((*available[day], day.year, day.month), []).append(day)

    from ecmwf.datastores import Client
    from ecmwf.datastores.processing import ProcessingFailedError

    client = Client(url=_CDS_URL, key=_key())
    print(
        f"ascat-fetch: {_DATASET} ACTIVE {start_time}..{end_time}: submitting "
        f"{len(groups)} CDS request(s)",
        file=sys.stderr,
    )
    remotes = []
    for (version, record, year, month), days in groups.items():
        request = {
            "variable": ["surface_soil_moisture_saturation"],
            "type_of_sensor": ["active"],
            "time_aggregation": ["daily"],
            "type_of_record": [record],
            "version": [version],
            "year": [str(year)],
            "month": [f"{month:02d}"],
            "day": [f"{d.day:02d}" for d in days],
        }
        try:
            remotes.append(client.submit(_DATASET, request))
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 403 and "licence" in str(exc).lower():
                raise DataError(
                    f"ERROR: CDS retrieval blocked: licence not accepted on {_DATASET}.\n"
                    f"Action: open https://{_CDS_HOST}/datasets/{_DATASET}?tab=download"
                    "#manage-licences in a browser, log in, accept the licence, then "
                    "re-run this skill.",
                    prefix=False,
                ) from None
            if status in (401, 403):
                raise DataError(
                    f"CDS rejected the key (HTTP {status}); check CDS_API_KEY / ~/.cdsapirc."
                ) from None
            raise DataError(f"CDS submit failed for {_DATASET} ({exc}).") from None

    waited = 0
    while True:
        try:
            if all(r.results_ready for r in remotes):
                break
        except ProcessingFailedError as exc:
            raise DataError(f"CDS reported no data for {_DATASET} ({exc}).") from None
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"polling the CDS for {_DATASET} failed ({exc}).") from None
        if waited >= _POLL_MAX_SECONDS:
            raise DataError(
                f"CDS job was still queued after {_POLL_MAX_SECONDS}s; re-run later or "
                "request fewer days."
            )
        time.sleep(_POLL_SECONDS)
        waited += _POLL_SECONDS

    names = ["sm", "sm_uncertainty"] if uncertainty else ["sm"]
    pieces = []
    with tempfile.TemporaryDirectory(prefix="ascat-fetch-") as tmp:
        for i, remote in enumerate(remotes):
            path = Path(tmp) / f"r{i}.zip"
            print(f"ascat-fetch: downloading {path.name}", file=sys.stderr)
            remote.download(str(path))
            # A request returns a zip of daily netCDFs, or one netCDF for a single day.
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(Path(tmp) / f"r{i}")
                files = sorted((Path(tmp) / f"r{i}").glob("*.nc"))
            else:
                files = [path]
            for f in files:
                with xr.open_dataset(f) as day:
                    day = day[names].rename(lat="latitude", lon="longitude").sortby("latitude")
                    day = ensure_normalized_longitude(day)
                    if bbox is not None:
                        day = bbox_subset(day, bbox, lat_dim="latitude", lon_dim="longitude")
                    pieces.append(day.load())
    if not pieces:
        raise DataError("the CDS returned no files.")

    ds = xr.concat(pieces, dim="time").sortby("time")
    ds = ds.rename({k: _VARS[k][0] for k in names})
    for k in names:
        name, long_name = _VARS[k]
        ds[name] = ds[name].astype("float32")
        ds[name].attrs = {
            "units": "percent",
            "long_name": long_name,
            "valid_min": np.float32(0),
            "valid_max": np.float32(100),
            "comment": "Relative to the wettest the soil gets at that cell (100 = saturated), "
            "top ~2 cm. Not volumetric (m3 m-3).",
        }
    ds.attrs = {
        "Conventions": "CF-1.13",
        "title": "C3S satellite surface soil moisture, ACTIVE (scatterometer: Metop ASCAT, ERS AMI)",
        "weather_skills_source": f"cds:{_DATASET}:active",
        "institution": "TU Wien / EODC for the Copernicus Climate Change Service",
        "source": f"{_CDS_URL} ({_DATASET}, versions "
        + ", ".join(sorted({v for v, *_ in groups}))
        + ")",
        "references": "https://doi.org/10.24381/cds.d7782f18",
        "license": "Copernicus Data License (CC-BY-4.0)",
    }
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

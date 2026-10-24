# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
#   "cftime",
#   "ecmwf-datastores-client==0.4.2",
#   "requests",
#   "xarray",
#   "cfgrib",
#   # eccodeslib carries the native libeccodes that cfgrib loads on macOS/Linux;
#   # eccodes drops this as a transitive dep via PyPI metadata (ecmwf/eccodes-python#150),
#   # so it is declared directly. Windows bundles the library in eccodes itself.
#   "eccodeslib",
#   "zarr",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch CAMS global air-quality fields (forecast or EAC4 reanalysis) over a bbox from the Copernicus ADS."""

# The forecast is 0.4 deg, 00/12 UTC inits, 5-day leads, 2015 on; EAC4 is
# 0.75 deg, 3-hourly, 2003 on. The bbox is cut server-side via the ADS `area`
# key. Requests go through the ADS retrieve API (the one cdsapi uses) with a
# personal key: ADS_API_KEY, or a ~/.cdsapirc whose `url:` points at the ADS.

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# The ADS API base URL is a fixed, public endpoint, not a secret.
_ADS_URL = "https://ads.atmosphere.copernicus.eu/api"
_ADS_HOST = "ads.atmosphere.copernicus.eu"

_DATASETS = {
    "forecast": "cams-global-atmospheric-composition-forecasts",
    "eac4": "cams-global-reanalysis-eac4",
}
# Lowest model level (~10 m above ground) = "surface" concentration of gases.
_LOWEST_LEVEL = {"forecast": "137", "eac4": "60"}
_FIRST_DAY = {"forecast": dt.date(2015, 1, 1), "eac4": dt.date(2003, 1, 1)}
_EAC4_TIMES = [f"{h:02d}:00" for h in range(0, 24, 3)]
_MAX_LEAD_HOURS = 120
_WARN_EAC4_DAYS = 366

_POLL_SECONDS = 10
_POLL_MAX_SECONDS = 3600

_M_AIR = 28.9647  # g/mol, dry air

# Keep dim-like coords; drop GRIB filter scalars (hybrid, surface, …) that collide.
_KEEP_COORDS = frozenset({"time", "step", "latitude", "longitude", "valid_time"})


class _Var(NamedTuple):
    ads: str  # ADS form name
    grib: tuple[str, ...]  # cfgrib short names that may come back
    kind: str  # pm | gas | aod | column
    long_name: str
    standard_name: str
    molar_mass: float = 0.0  # g/mol, gases only


# Most used first. Tokens match openaq-fetch where the quantity is the same.
VARIABLES: dict[str, _Var] = {
    "pm25": _Var(
        "particulate_matter_2.5um",
        ("pm2p5",),
        "pm",
        "PM2.5 mass concentration",
        "mass_concentration_of_pm2p5_ambient_aerosol_particles_in_air",
    ),
    "pm10": _Var(
        "particulate_matter_10um",
        ("pm10",),
        "pm",
        "PM10 mass concentration",
        "mass_concentration_of_pm10_ambient_aerosol_particles_in_air",
    ),
    "no2": _Var(
        "nitrogen_dioxide",
        ("no2",),
        "gas",
        "Near-surface NO2 mole fraction (lowest model level)",
        "mole_fraction_of_nitrogen_dioxide_in_air",
        46.0055,
    ),
    "o3": _Var(
        "ozone",
        ("go3", "o3"),
        "gas",
        "Near-surface O3 mole fraction (lowest model level)",
        "mole_fraction_of_ozone_in_air",
        47.9982,
    ),
    "aod550": _Var(
        "total_aerosol_optical_depth_550nm",
        ("aod550",),
        "aod",
        "Total aerosol optical depth at 550 nm",
        "atmosphere_optical_thickness_due_to_ambient_aerosol_particles",
    ),
    "so2": _Var(
        "sulphur_dioxide",
        ("so2",),
        "gas",
        "Near-surface SO2 mole fraction (lowest model level)",
        "mole_fraction_of_sulfur_dioxide_in_air",
        64.066,
    ),
    "co": _Var(
        "carbon_monoxide",
        ("co",),
        "gas",
        "Near-surface CO mole fraction (lowest model level)",
        "mole_fraction_of_carbon_monoxide_in_air",
        28.0101,
    ),
    "pm1": _Var(
        "particulate_matter_1um",
        ("pm1",),
        "pm",
        "PM1 mass concentration",
        "mass_concentration_of_pm1_ambient_aerosol_particles_in_air",
    ),
    "duaod550": _Var(
        "dust_aerosol_optical_depth_550nm",
        ("duaod550",),
        "aod",
        "Dust aerosol optical depth at 550 nm",
        "atmosphere_optical_thickness_due_to_dust_ambient_aerosol_particles",
    ),
    "tcno2": _Var(
        "total_column_nitrogen_dioxide",
        ("tcno2",),
        "column",
        "Total column NO2",
        "atmosphere_mass_content_of_nitrogen_dioxide",
    ),
    "tco3": _Var(
        "total_column_ozone",
        ("gtco3", "tco3"),
        "column",
        "Total column ozone",
        "atmosphere_mass_content_of_ozone",
    ),
    "tcco": _Var(
        "total_column_carbon_monoxide",
        ("tcco",),
        "column",
        "Total column CO",
        "atmosphere_mass_content_of_carbon_monoxide",
    ),
    "tcso2": _Var(
        "total_column_sulphur_dioxide",
        ("tcso2",),
        "column",
        "Total column SO2",
        "atmosphere_mass_content_of_sulfur_dioxide",
    ),
}
_UNITS = {"pm": "ug m-3", "gas": "nmol mol-1", "aod": "1", "column": "kg m-2"}


@weather_skill(name="cams-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--bbox", required=True)
@weather_skill.argument(
    "--dataset",
    choices=tuple(_DATASETS),
    default="forecast",
    help=(
        "forecast: CAMS global composition forecast (0.4 deg, 2015+, needs --date). "
        "eac4: CAMS global reanalysis (0.75 deg, 3-hourly, 2003+, needs "
        "--start-time/--end-time). Default forecast."
    ),
)
@weather_skill.argument("--date")
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    choices=tuple(VARIABLES),
    help="Field to fetch; repeat for more. Default pm25.",
)
@weather_skill.argument(
    "--cycle",
    type=int,
    choices=(0, 12),
    default=0,
    help="Forecast init hour UTC: 0 or 12. Default 0.",
)
@weather_skill.argument(
    "--max-lead",
    default=f"{_MAX_LEAD_HOURS}h",
    help=f"Forecast: last lead, as hours (48) or a timedelta (48h, 2D), up to "
    f"{_MAX_LEAD_HOURS}h. Default {_MAX_LEAD_HOURS}h. Leads are 3-hourly.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the last day the ADS catalogue lists for the product (YYYY-MM-DD) "
        "and exit. IDENT is `forecast` or `eac4` (default: --dataset). No key needed."
    ),
)
def fetch(
    bbox,
    dataset,
    date,
    start_time,
    end_time,
    variable,
    cycle,
    max_lead,
    **kwargs,
):
    """Fetch CAMS global air-quality fields (forecast or EAC4 reanalysis) over a bbox from the Copernicus ADS."""
    import numpy as np
    import pandas as pd
    import requests
    import xarray as xr

    probe = kwargs.get("probe_latest")
    if probe is not None:
        product = probe or dataset
        if product not in _DATASETS:
            raise UsageError(f"--probe-latest IDENT must be one of: {', '.join(_DATASETS)}.")
        url = f"{_ADS_URL}/catalogue/v1/collections/{_DATASETS[product]}"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            last = resp.json()["extent"]["temporal"]["interval"][0][1]
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"could not read the ADS catalogue for {product} ({exc}).") from None
        print(last[:10] if last else "none")
        return None

    names = list(dict.fromkeys(variable or ["pm25"]))

    dataset_id = _DATASETS[dataset]
    base = {"data_format": "grib"}
    if dataset == "forecast":
        if date is None or start_time is not None or end_time is not None:
            raise UsageError("--dataset forecast takes --date (the init day), not a range.")
        if date < _FIRST_DAY["forecast"]:
            raise UsageError("the CAMS global forecast on the ADS starts 2015-01-01.")
        try:
            hours = pd.Timedelta(f"{max_lead}h" if max_lead.isdigit() else max_lead) / pd.Timedelta(
                1, "h"
            )
        except ValueError:
            raise UsageError(f"--max-lead {max_lead!r} is not a lead (48, 48h, 2D).") from None
        if not 0 <= hours <= _MAX_LEAD_HOURS:
            raise UsageError(f"--max-lead must be within 0..{_MAX_LEAD_HOURS}h.")
        base.update(
            type=["forecast"],
            time=[f"{cycle:02d}:00"],
            leadtime_hour=[str(h) for h in range(0, int(hours) + 1, 3)],
        )
        chunks = [(date, date)]
        label = f"{date.isoformat()} {cycle:02d}Z"
    else:
        if date is not None or start_time is None or end_time is None:
            raise UsageError("--dataset eac4 takes --start-time and --end-time, not --date.")
        if start_time < _FIRST_DAY["eac4"]:
            raise UsageError("EAC4 starts 2003-01-01.")
        base["time"] = list(_EAC4_TIMES)
        # One ADS request per calendar month.
        chunks = []
        cur = start_time
        while cur <= end_time:
            nxt = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
            chunks.append((cur, min(end_time, nxt - dt.timedelta(days=1))))
            cur = nxt
        label = f"{start_time.isoformat()}..{end_time.isoformat()}"
        days = (end_time - start_time).days + 1
        if days > _WARN_EAC4_DAYS:
            print(
                f"cams-fetch: warning: {days} days of 3-hourly EAC4 is {len(chunks)} "
                "ADS requests; expect a long queue.",
                file=sys.stderr,
            )

    # Credentials: ADS_API_KEY, else a ~/.cdsapirc whose url is the ADS.
    key = os.environ.get("ADS_API_KEY")
    rc = Path("~/.cdsapirc").expanduser()
    if not key and rc.is_file():
        fields = dict(line.partition(":")[::2] for line in rc.read_text().splitlines())
        fields = {k.strip(): v.strip() for k, v in fields.items()}
        if _ADS_HOST in fields.get("url", ""):
            key = fields.get("key")
    if not key:
        raise UsageError(
            "no ADS credentials: set ADS_API_KEY (your personal token from "
            "https://ads.atmosphere.copernicus.eu/profile), or write a ~/.cdsapirc whose "
            f"url is {_ADS_URL}."
        )

    # Single-level fields and lowest-model-level gases need separate requests,
    # and a box crossing the antimeridian (W > E) is fetched as two boxes.
    gases = [n for n in names if VARIABLES[n].kind == "gas"]
    groups = [g for g in ([n for n in names if n not in gases], gases) if g]
    north, west, south, east = bbox
    areas = (
        [list(bbox)]
        if west <= east
        else [[north, west, south, 180.0], [north, -180.0, south, east]]
    )

    from ecmwf.datastores import Client
    from ecmwf.datastores.processing import ProcessingFailedError

    client = Client(url=_ADS_URL, key=key)
    # jobs[chunk][group] = one ADS job per area; all submitted before waiting.
    jobs = []
    print(
        f"cams-fetch: {dataset_id} {label} area={list(bbox)} variables={','.join(names)}: "
        f"submitting {len(chunks) * len(groups) * len(areas)} ADS request(s)",
        file=sys.stderr,
    )
    for first, last in chunks:
        jobs.append([])
        for group in groups:
            jobs[-1].append([])
            for area in areas:
                request = {
                    **base,
                    "variable": [VARIABLES[n].ads for n in group],
                    "date": [f"{first.isoformat()}/{last.isoformat()}"],
                    "area": area,
                }
                if group is gases:
                    request["model_level"] = [_LOWEST_LEVEL[dataset]]
                try:
                    jobs[-1][-1].append(client.submit(dataset_id, request))
                except requests.HTTPError as exc:
                    status = getattr(exc.response, "status_code", None)
                    if status == 403 and "licence" in str(exc).lower():
                        raise DataError(
                            f"ERROR: ADS retrieval blocked: licence not accepted on {dataset_id}.\n"
                            f"Action: open https://{_ADS_HOST}/datasets/{dataset_id}?tab=download"
                            "#manage-licences in a browser, log in, accept the licence, then "
                            "re-run this skill.",
                            prefix=False,
                        ) from None
                    if status in (401, 403):
                        raise DataError(
                            f"ADS rejected the key (HTTP {status}); check ADS_API_KEY / ~/.cdsapirc."
                        ) from None
                    raise DataError(f"ADS submit failed for {dataset_id} ({exc}).") from None

    remotes = [r for chunk in jobs for group in chunk for r in group]
    waited = 0
    while True:
        try:
            if all(r.results_ready for r in remotes):
                break
        except ProcessingFailedError as exc:
            raise DataError(
                f"ADS reported no data for {dataset_id} {label} ({exc}); check the date is "
                "within the product's range (`--probe-latest`) and the variable exists for it."
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"polling ADS for {dataset_id} {label} failed ({exc}).") from None
        if waited >= _POLL_MAX_SECONDS:
            raise DataError(
                f"ADS job for {dataset_id} {label} was still queued after {_POLL_MAX_SECONDS}s; "
                "the ADS queue is busy. Re-run later or request less."
            )
        time.sleep(_POLL_SECONDS)
        waited += _POLL_SECONDS

    import cfgrib

    pieces = []
    with tempfile.TemporaryDirectory(prefix="cams-fetch-") as tmp:
        for ci, chunk in enumerate(jobs):
            fields = []
            for gi, (group, group_jobs) in enumerate(zip(groups, chunk, strict=True)):
                legs = []
                for ai, remote in enumerate(group_jobs):
                    path = Path(tmp) / f"c{ci}g{gi}a{ai}.grib"
                    print(f"cams-fetch: downloading {path.name}", file=sys.stderr)
                    remote.download(str(path))
                    # A GRIB can decode to several datasets (one per level type);
                    # drop the GRIB filter scalars (hybrid, surface, ...) that collide.
                    parts = cfgrib.open_datasets(str(path))
                    if not parts:
                        raise DataError(f"ADS GRIB {path.name} contained no messages.")
                    parts = [
                        p.drop_vars(
                            [c for c in p.coords if c not in p.dims and c not in _KEEP_COORDS]
                        )
                        for p in parts
                    ]
                    legs.append(
                        ensure_normalized_longitude(
                            xr.merge(parts, compat="override", combine_attrs="override")
                        )
                    )
                ds = legs[0]
                if len(legs) > 1:
                    # The antimeridian legs end at 180 and start at -180: keep one seam.
                    ds = xr.concat(legs, dim="longitude")
                    lon = ds["longitude"]
                    ds = ds.assign_coords(
                        longitude=lon.copy(data=np.where(lon == 180.0, -180.0, lon))
                    )
                    _, idx = np.unique(ds["longitude"].values, return_index=True)
                    ds = ds.isel(longitude=np.sort(idx)).sortby("longitude")
                # cfgrib short names -> -v tokens.
                found = {}
                for name in group:
                    grib = VARIABLES[name].grib
                    hit = next(
                        (
                            v
                            for v in ds.data_vars
                            if v in grib or ds[v].attrs.get("GRIB_shortName") in grib
                        ),
                        None,
                    )
                    if hit is None:
                        raise DataError(
                            f"ADS returned no field for {name} ({VARIABLES[name].ads}); got "
                            + ", ".join(map(str, ds.data_vars))
                        )
                    found[hit] = name
                fields.append(ds[list(found)].rename(found))
            ds = xr.merge(fields, join="outer", compat="override")
            # Forecast: (step, lat, lon) with a scalar init `time`. EAC4: (time, lat, lon).
            if dataset == "forecast":
                if "step" not in ds.dims:
                    ds = ds.expand_dims("step")
            else:
                if ds.sizes.get("step", 1) != 1:
                    raise DataError("EAC4 GRIB unexpectedly carried several steps.")
                ds = ds.squeeze("step", drop=True) if "step" in ds.dims else ds
                ds = ds.drop_vars(["step", "valid_time"], errors="ignore")
                if "time" not in ds.dims:
                    ds = ds.expand_dims("time")
            # Materialize while the GRIB files in the temp dir still exist.
            pieces.append(ds.load())
    ds = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
    ds = ds[names]

    # Air-quality units (ug m-3, nmol mol-1) and CF attrs.
    for name in names:
        spec = VARIABLES[name]
        da = ds[name].astype("float32")
        if spec.kind == "pm":
            da = da * 1e9  # kg m-3 -> ug m-3
        elif spec.kind == "gas":
            da = da * (_M_AIR / spec.molar_mass) * 1e9  # kg kg-1 -> nmol mol-1 (ppb)
        da.attrs = {k: v for k, v in ds[name].attrs.items() if not k.startswith("GRIB_")}
        da.attrs.update(
            units=_UNITS[spec.kind],
            long_name=spec.long_name,
            standard_name=spec.standard_name,
            ads_variable=spec.ads,
        )
        if spec.kind == "gas":
            da.attrs["model_level"] = int(_LOWEST_LEVEL[dataset])
            da.attrs["comment"] = "Lowest model level (~10 m above ground)."
        ds[name] = da
    ds = ensure_normalized_longitude(ds)
    ds.attrs = {
        "Conventions": "CF-1.13",
        "weather_skills_source": f"cams:{dataset_id}",
        "institution": "ECMWF / Copernicus Atmosphere Monitoring Service",
        "source": f"{_ADS_URL} ({dataset_id})",
    }
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()

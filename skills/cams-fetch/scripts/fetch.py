# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
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
"""Fetch CAMS global air-quality fields from the Copernicus Atmosphere Data Store (ADS) for a bbox.

Two products: the CAMS global atmospheric composition **forecast** (0.4 deg,
00/12 UTC inits, 5-day leads, 2015 onward) and the **EAC4** global reanalysis
(0.75 deg, 3-hourly, 2003 onward). The bbox is cut server-side via the ADS
`area` key, so only the region is downloaded. Requests go through the ADS
retrieve API (the same API `cdsapi` talks to) with a personal ADS key.

Credentials: ADS_API_KEY, or a ~/.cdsapirc whose `url:` points at the ADS.
"""

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
_SKILL_VERSION = "0.0.1"

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
_BY_ADS_NAME = {v.ads: k for k, v in VARIABLES.items()}


def _resolve_variables(raw) -> list[str]:
    """Flatten `-v a b`, `-v a -v b`, `-v a,b`; accept ADS form names too."""
    tokens: list[str] = []
    for item in raw or []:
        for part in item if isinstance(item, list) else [item]:
            tokens.extend(t.strip() for t in str(part).split(",") if t.strip())
    if not tokens:
        tokens = ["pm25"]
    out: list[str] = []
    for token in tokens:
        name = token.lower()
        name = _BY_ADS_NAME.get(name, name)
        if name not in VARIABLES:
            raise UsageError(
                f"unknown variable {token!r}. Available (most used first): " + ", ".join(VARIABLES)
            )
        if name not in out:
            out.append(name)
    return out


def _api_key() -> str:
    key = os.environ.get("ADS_API_KEY")
    if key:
        return key
    rc = Path("~/.cdsapirc").expanduser()
    if rc.is_file():
        fields = {}
        for line in rc.read_text().splitlines():
            k, sep, v = line.partition(":")
            if sep:
                fields[k.strip()] = v.strip()
        if _ADS_HOST in fields.get("url", "") and fields.get("key"):
            return fields["key"]
    raise UsageError(
        "no ADS credentials: set ADS_API_KEY (your personal token from "
        "https://ads.atmosphere.copernicus.eu/profile), or write a ~/.cdsapirc whose "
        f"url is {_ADS_URL}."
    )


def _lead_hours(max_lead: int, lead_step: int) -> list[str]:
    if not 0 <= max_lead <= _MAX_LEAD_HOURS:
        raise UsageError(f"--max-lead-hours must be 0..{_MAX_LEAD_HOURS}.")
    if lead_step < 1:
        raise UsageError("--lead-step must be a positive number of hours.")
    return [str(h) for h in range(0, max_lead + 1, lead_step)]


def _month_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Split [start, end] at calendar-month boundaries (one ADS request each)."""
    out = []
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        out.append((cur, min(end, nxt - dt.timedelta(days=1))))
        cur = nxt
    return out


def _split_wrapped_area(area: list[float]) -> list[list[float]]:
    """Split antimeridian-crossing [N, W, S, E] (W > E) into two valid areas."""
    n, w, s, e = area
    if w <= e:
        return [area]
    return [[n, w, s, 180.0], [n, -180.0, s, e]]


def _groups(names: list[str]) -> list[tuple[bool, list[str]]]:
    """Single-level fields and lowest-model-level gases go in separate requests."""
    single = [n for n in names if VARIABLES[n].kind != "gas"]
    gases = [n for n in names if VARIABLES[n].kind == "gas"]
    return [(False, single), (True, gases)] if single and gases else [(bool(gases), names)]


def _build_request(
    dataset: str,
    names: list[str],
    area: list[float],
    *,
    date_range: tuple[dt.date, dt.date],
    model_level: bool,
    cycle: str = "00",
    leads: list[str] | None = None,
) -> dict:
    start, end = date_range
    req = {
        "variable": [VARIABLES[n].ads for n in names],
        "date": [f"{start.isoformat()}/{end.isoformat()}"],
        "area": area,
        "data_format": "grib",
    }
    if dataset == "forecast":
        req.update(type=["forecast"], time=[f"{cycle}:00"], leadtime_hour=leads)
    else:
        req["time"] = list(_EAC4_TIMES)
    if model_level:
        req["model_level"] = [_LOWEST_LEVEL[dataset]]
    return req


def _licence_url(dataset_id: str) -> str:
    return f"https://{_ADS_HOST}/datasets/{dataset_id}?tab=download#manage-licences"


def _submit(client, dataset_id: str, request: dict):
    import requests

    try:
        return client.submit(dataset_id, request)
    except requests.HTTPError as exc:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status == 403 and "licence" in str(exc).lower():
            raise DataError(
                f"ERROR: ADS retrieval blocked: licence not accepted on {dataset_id}.\n"
                f"Action: open {_licence_url(dataset_id)} in a browser, log in, accept "
                "the licence, then re-run this skill.",
                prefix=False,
            ) from None
        if status in (401, 403):
            raise DataError(
                f"ADS rejected the key (HTTP {status}); check ADS_API_KEY / ~/.cdsapirc."
            ) from None
        raise DataError(f"ADS submit failed for {dataset_id} ({exc}).") from None


def _wait(remotes, label: str) -> None:
    from ecmwf.datastores.processing import ProcessingFailedError

    waited = 0
    while True:
        try:
            if all(r.results_ready for r in remotes):
                return
        except ProcessingFailedError as exc:
            raise DataError(
                f"ADS reported no data for {label} ({exc}); check the date is within the "
                "product's range (`--probe-latest`) and the variable exists for it."
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"polling ADS for {label} failed ({exc}).") from None
        if waited >= _POLL_MAX_SECONDS:
            raise DataError(
                f"ADS job for {label} was still queued after {_POLL_MAX_SECONDS}s; "
                "the ADS queue is busy. Re-run later or request less."
            )
        time.sleep(_POLL_SECONDS)
        waited += _POLL_SECONDS


def _open_grib(path: Path):
    """Decode a (possibly mixed-level) GRIB into one Dataset, minus filter coords."""
    import cfgrib
    import xarray as xr

    parts = cfgrib.open_datasets(str(path))
    if not parts:
        raise DataError(f"ADS GRIB {path.name} contained no messages.")
    cleaned = []
    for part in parts:
        drop = [c for c in part.coords if c not in part.dims and c not in _KEEP_COORDS]
        cleaned.append(part.drop_vars(drop))
    if len(cleaned) == 1:
        return cleaned[0]
    return xr.merge(cleaned, compat="override", combine_attrs="override")


def _concat_lon(datasets: list):
    """Concatenate per-area datasets along longitude; drop the duplicated ±180 seam."""
    import numpy as np
    import xarray as xr

    if len(datasets) == 1:
        return datasets[0]
    normed = [ensure_normalized_longitude(d) for d in datasets]
    combined = xr.concat(normed, dim="longitude")
    _, idx = np.unique(combined["longitude"].values, return_index=True)
    return combined.isel(longitude=np.sort(idx)).sortby("longitude")


def _rename_to_tokens(ds, names: list[str]):
    """Map cfgrib short names onto `-v` tokens and keep only the requested fields."""
    found = {}
    for name in names:
        hit = next((g for g in VARIABLES[name].grib if g in ds.data_vars), None)
        if hit is None:
            hit = next(
                (
                    v
                    for v in ds.data_vars
                    if ds[v].attrs.get("GRIB_shortName") in VARIABLES[name].grib
                ),
                None,
            )
        if hit is None:
            raise DataError(
                f"ADS returned no field for {name} ({VARIABLES[name].ads}); got "
                + ", ".join(map(str, ds.data_vars))
            )
        found[hit] = name
    return ds[list(found)].rename(found)


def _shape_axes(ds, dataset: str):
    """Forecast -> (step, lat, lon) with scalar init `time`; EAC4 -> (time, lat, lon)."""
    if dataset == "forecast":
        if "step" not in ds.dims:
            ds = ds.expand_dims("step")
        return ds
    if "step" in ds.dims:
        if ds.sizes["step"] != 1:
            raise DataError("EAC4 GRIB unexpectedly carried several steps.")
        ds = ds.squeeze("step")
    ds = ds.drop_vars("step", errors="ignore")
    if "time" not in ds.dims:
        ds = ds.expand_dims("time")
    return ds


def _standardize(ds, dataset: str, dataset_id: str):
    """Convert to AQ units (ug m-3, nmol mol-1) and stamp CF attrs."""
    for name in list(ds.data_vars):
        spec = VARIABLES[name]
        da = ds[name].astype("float32")
        if spec.kind == "pm":
            da = da * 1e9  # kg m-3 -> ug m-3
        elif spec.kind == "gas":
            da = da * (_M_AIR / spec.molar_mass) * 1e9  # kg kg-1 -> nmol mol-1 (ppb)
        attrs = {k: v for k, v in ds[name].attrs.items() if not k.startswith("GRIB_")}
        attrs.update(
            units=_UNITS[spec.kind],
            long_name=spec.long_name,
            standard_name=spec.standard_name,
            ads_variable=spec.ads,
        )
        if spec.kind == "gas":
            attrs["model_level"] = int(_LOWEST_LEVEL[dataset])
            attrs["comment"] = "Lowest model level (~10 m above ground)."
        da.attrs = attrs
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


def _probe_latest(dataset: str) -> str:
    import requests

    dataset_id = _DATASETS[dataset]
    url = f"{_ADS_URL}/catalogue/v1/collections/{dataset_id}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        end = resp.json()["extent"]["temporal"]["interval"][0][1]
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"could not read the ADS catalogue for {dataset_id} ({exc}).") from None
    return end[:10] if end else "none"


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
    nargs="+",
    help=(
        "Fields to fetch (several per flag, repeat, or comma-separate). Most used "
        "first: " + ", ".join(VARIABLES) + ". Default pm25."
    ),
)
@weather_skill.argument(
    "--cycle",
    choices=("00", "12"),
    default="00",
    help="Forecast init hour (UTC). Default 00.",
)
@weather_skill.argument(
    "--max-lead-hours",
    type=int,
    default=_MAX_LEAD_HOURS,
    help=f"Forecast: last lead hour to fetch (0..{_MAX_LEAD_HOURS}). Default {_MAX_LEAD_HOURS}.",
)
@weather_skill.argument(
    "--lead-step",
    type=int,
    default=3,
    help=(
        "Forecast: lead spacing in hours. Default 3. 1 works for single-level "
        "fields (pm*, aod*, tc*) only; gases (no2, o3, so2, co) are 3-hourly."
    ),
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
    max_lead_hours,
    lead_step,
    **kwargs,
):
    """Fetch CAMS global air-quality fields (forecast or EAC4) for a bbox from the ADS."""
    probe = kwargs.get("probe_latest")
    if probe is not None:
        product = probe or dataset
        if product not in _DATASETS:
            raise UsageError(f"--probe-latest IDENT must be one of: {', '.join(_DATASETS)}.")
        print(_probe_latest(product))
        return None

    names = _resolve_variables(variable)
    dataset_id = _DATASETS[dataset]
    if dataset == "forecast":
        if date is None or start_time is not None or end_time is not None:
            raise UsageError("--dataset forecast takes --date (the init day), not a range.")
        if date < _FIRST_DAY["forecast"]:
            raise UsageError("the CAMS global forecast on the ADS starts 2015-01-01.")
        leads = _lead_hours(max_lead_hours, lead_step)
        chunks = [(date, date)]
        label = f"{date.isoformat()} {cycle}Z"
    else:
        if date is not None or start_time is None or end_time is None:
            raise UsageError("--dataset eac4 takes --start-time and --end-time, not --date.")
        if start_time < _FIRST_DAY["eac4"]:
            raise UsageError("EAC4 starts 2003-01-01.")
        leads = None
        chunks = _month_chunks(start_time, end_time)
        label = f"{start_time.isoformat()}..{end_time.isoformat()}"
        days = (end_time - start_time).days + 1
        if days > _WARN_EAC4_DAYS:
            print(
                f"cams-fetch: warning: {days} days of 3-hourly EAC4 is {len(chunks)} "
                "ADS requests; expect a long queue.",
                file=sys.stderr,
            )

    key = _api_key()

    import xarray as xr
    from ecmwf.datastores import Client

    areas = _split_wrapped_area(list(bbox))
    legs = []
    for ci, rng in enumerate(chunks):
        for gi, (model_level, group) in enumerate(_groups(names)):
            for ai, area in enumerate(areas):
                req = _build_request(
                    dataset,
                    group,
                    area,
                    date_range=rng,
                    model_level=model_level,
                    cycle=cycle,
                    leads=leads,
                )
                legs.append({"chunk": ci, "group": gi, "area": ai, "names": group, "req": req})

    print(
        f"cams-fetch: {dataset_id} {label} area={list(bbox)} variables={','.join(names)}: "
        f"submitting {len(legs)} ADS request(s)",
        file=sys.stderr,
    )
    client = Client(url=_ADS_URL, key=key)
    for leg in legs:
        leg["remote"] = _submit(client, dataset_id, leg["req"])
    _wait([leg["remote"] for leg in legs], f"{dataset_id} {label}")

    with tempfile.TemporaryDirectory(prefix="cams-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        for i, leg in enumerate(legs):
            leg["grib"] = tmp / f"leg{i}.grib"
            print(f"cams-fetch: downloading {leg['grib'].name}", file=sys.stderr)
            leg["remote"].download(str(leg["grib"]))

        pieces = []
        for ci in range(len(chunks)):
            groups = []
            for gi, (_, group) in enumerate(_groups(names)):
                parts = [
                    _open_grib(leg["grib"])
                    for leg in legs
                    if leg["chunk"] == ci and leg["group"] == gi
                ]
                groups.append(_rename_to_tokens(_concat_lon(parts), group))
            merged = xr.merge(groups, join="outer", compat="override")
            pieces.append(_shape_axes(merged, dataset))
        ds = pieces[0] if len(pieces) == 1 else xr.concat(pieces, dim="time")
        ds = ds[names]
        if "valid_time" in ds.coords and dataset == "eac4":
            ds = ds.drop_vars("valid_time")
        # Materialize while the GRIB files in the temp dir still exist.
        ds = ds.load()

    return _standardize(ds, dataset, dataset_id)


if __name__ == "__main__":
    fetch()

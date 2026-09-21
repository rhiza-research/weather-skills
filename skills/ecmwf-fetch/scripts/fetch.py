# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "dynamical-catalog==1.0.1",
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
"""Fetch ECMWF S2S ensemble fields (cf + pf) and write a weather-skills standard dataset Zarr."""

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
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    np_to_date,
    require_env,
)
from weather_skills_core.units import (
    convert_dataarray,
    precip_amounts_to_rates,
    stamp_data_interval,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_PROBE_POLL_SECONDS = 30
_PROBE_POLL_MAX_SECONDS = 3600

# S2S archives tp 6-hourly; request 00Z daily leads through the 46-day S2S range.
_MAX_LEAD_HOURS = 46 * 24
LEADTIME_HOURS = [str(h) for h in range(0, _MAX_LEAD_HOURS + 1, 24)]

S2S_LICENCE_URL = "https://ecds.ecmwf.int/datasets/s2s-forecasts?tab=download#manage-licences"

# Embargo detection: match this phrase on the exception chain (MarsRuntimeError
# is not reliably importable from ecmwf.datastores). Keep narrow so generic
# access/auth failures do not classify as embargo.
_S2S_EMBARGO_SIGNATURES = ("restricted access to s2s",)
_EMBARGO_CHAIN_MAX_DEPTH = 8

# Keep dim-like coords; drop GRIB filter scalars that collide across parameters.
_KEEP_COORDS = frozenset(
    {
        "time",
        "step",
        "latitude",
        "longitude",
        "number",
        "valid_time",
        "lat",
        "lon",
        "vertical",
        "isobaricInhPa",
        "isobaricInPa",
        "level",
        "theta",
    }
)

PRESSURE_LEVELS_10 = ("1000", "925", "850", "700", "500", "300", "200", "100", "50", "10")
PRESSURE_LEVELS_7 = ("1000", "925", "850", "700", "500", "300", "200")


class _Var(NamedTuple):
    ecds: str
    family: str  # instant = integer hours; daily = 24 h averaging windows
    level_type: str = "single_level"
    levels: tuple[str, ...] = ()
    level_key: str = "pressure_level"
    control_only: bool = False


# Most-used first, then the rest of S2S single-level + ocean. `-v` / Zarr
# tokens are cfgrib short names (sst, not ARCO 2m_temperature).
VARIABLES: dict[str, _Var] = {
    "tp": _Var("total_precipitation", "instant"),
    "t2m": _Var("2_m_temperature", "daily"),
    "sst": _Var("sea_surface_temperature", "daily"),
    "d2m": _Var("2_m_dewpoint_temperature", "daily"),
    "mx2t6": _Var("maximum_2_m_temperature_in_the_last_6_hours", "instant"),
    "mn2t6": _Var("minimum_2_m_temperature_in_the_last_6_hours", "instant"),
    "u10": _Var("10_m_u_component_of_wind", "instant"),
    "v10": _Var("10_m_v_component_of_wind", "instant"),
    "msl": _Var("mean_sea_level_pressure", "instant"),
    "cape": _Var("convective_available_potential_energy", "daily"),
    "tcw": _Var("total_column_water", "daily"),
    "gh": _Var(
        "geopotential_height",
        "instant",
        "pressure_level",
        PRESSURE_LEVELS_10,
        control_only=True,
    ),
    "t": _Var("temperature", "instant", "pressure_level", PRESSURE_LEVELS_10, control_only=True),
    "u": _Var(
        "u_component_of_wind",
        "instant",
        "pressure_level",
        PRESSURE_LEVELS_10,
        control_only=True,
    ),
    "v": _Var(
        "v_component_of_wind",
        "instant",
        "pressure_level",
        PRESSURE_LEVELS_10,
        control_only=True,
    ),
    "w": _Var(
        "vertical_velocity",
        "instant",
        "pressure_level",
        PRESSURE_LEVELS_10,
        control_only=True,
    ),
    "q": _Var(
        "specific_humidity",
        "instant",
        "pressure_level",
        PRESSURE_LEVELS_7,
        control_only=True,
    ),
    "pv": _Var(
        "potential_vorticity",
        "instant",
        "potential_temperature",
        ("320",),
        "potential_temperature",
        True,
    ),
    "skt": _Var("skin_temperature", "daily"),
    "tcc": _Var("total_cloud_cover", "daily"),
    "sp": _Var("surface_pressure", "instant"),
    "lsm": _Var("land_sea_mask", "instant"),
    "orog": _Var("orography", "instant"),
    "slt": _Var("soil_type", "instant"),
    "sd": _Var("snow_depth_water_equivalent", "daily"),
    "rsn": _Var("snow_density", "daily"),
    "asn": _Var("snow_albedo", "daily"),
    "sm20": _Var("soil_moisture_top_20_cm", "daily"),
    "sm100": _Var("soil_moisture_top_100_cm", "daily"),
    "st20": _Var("soil_temperature_top_20_cm", "daily"),
    "st100": _Var("soil_temperature_top_100_cm", "daily"),
    "ci": _Var("sea_ice_area_fraction", "daily"),
    "sshf": _Var("surface_sensible_heat_flux", "instant"),
    "slhf": _Var("surface_latent_heat_flux", "instant"),
    "ssr": _Var("surface_net_solar_radiation", "instant"),
    "ssrd": _Var("surface_solar_radiation_downwards", "instant"),
    "str": _Var("surface_net_thermal_radiation", "instant"),
    "strd": _Var("surface_thermal_radiation_downwards", "instant"),
    "ttr": _Var("top_net_thermal_radiation", "instant"),
    "cp": _Var("convective_precipitation", "instant"),
    "sf": _Var("snow_fall_water_equivalent", "instant"),
    "ewss": _Var("eastward_turbulent_surface_stress", "instant"),
    "nsss": _Var("northward_turbulent_surface_stress", "instant"),
    "ro": _Var("water_runoff_and_drainage", "instant"),
    "sro": _Var("surface_runoff", "instant"),
    "t20d": _Var("depth_of_20_C_isotherm", "daily"),
    "sav300": _Var("mean_sea_water_practical_salinity_in_the_upper_300_m", "daily"),
    "mswpt300": _Var("mean_sea_water_potential_temperature_in_the_upper_300_m", "daily"),
    "mlotst010": _Var("ocean_mixed_layer_thickness_defined_by_sigma_theta_0_01_kg_m_3", "daily"),
    "ocu": _Var("u_component_of_surface_current", "daily"),
    "ocv": _Var("v_component_of_surface_current", "daily"),
    "sithick": _Var("sea_ice_thickness", "daily"),
    "zos": _Var("sea_surface_height", "daily"),
    "sos": _Var("sea_surface_pratical_salinity", "daily"),  # ECDS spelling
}
DEFAULT_VARIABLES = ["tp"]

# dynamical.org ECMWF ER / S2S 46-day product. Inits start 2026-01-01; still
# ~2-day embargo. Prefer this over ECDS when every requested field maps.
_DATASET = "ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree"
_STAGING_CATALOG = "https://stac-staging.dynamical.org/catalog.json"
_CATALOG_START = dt.date(2026, 1, 1)
_PRESSURE_GROUP = "pressure_level"
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

# S2S short name → catalog field. Fluxes (ECDS accumulated J m-2 vs catalog
# mean W m-2), 6-hour max/min, static masks, ocean, and pv stay on ECDS.
_CATALOG_SURFACE = {
    "tp": "precipitation_surface",
    "t2m": "average_temperature_2m",
    "sst": "sea_surface_temperature",
    "d2m": "average_dew_point_temperature_2m",
    "u10": "wind_u_10m",
    "v10": "wind_v_10m",
    "msl": "pressure_reduced_to_mean_sea_level",
    "cape": "average_convective_available_potential_energy_atmosphere",
    "tcw": "total_column_water_atmosphere",
    "skt": "skin_temperature_surface",
    "tcc": "total_cloud_cover_atmosphere",
    "sp": "pressure_surface",
    "sd": "snow_water_equivalent_surface",
    "rsn": "snow_density_surface",
    "asn": "snow_albedo_surface",
    "sm20": "soil_moisture_0_20cm",
    "sm100": "soil_moisture_0_100cm",
    "st20": "soil_temperature_0_20cm",
    "st100": "soil_temperature_0_100cm",
    "ci": "sea_ice_area_fraction",
    "cp": "precipitation_convective_surface",
    "sf": "snowfall_water_equivalent_rate_surface",
    "ewss": "eastward_turbulent_surface_stress",
    "nsss": "northward_turbulent_surface_stress",
    "ro": "runoff_water_equivalent_surface",
    "sro": "runoff_surface",
}
_CATALOG_PRESSURE = {
    "gh": "geopotential_height",
    "t": "temperature",
    "u": "wind_u",
    "v": "wind_v",
    "w": "vertical_velocity",
    "q": "specific_humidity",
}
_CATALOG_TO_SHORT = {v: k for k, v in {**_CATALOG_SURFACE, **_CATALOG_PRESSURE}.items()}

# cfgrib / S2S abbreviations that are not the canonical `-v` token.
_GRIB_EXTRAS = {
    "2t": "t2m",
    "2d": "d2m",
    "10u": "u10",
    "10v": "v10",
    "wtmp": "sst",
    "z": "gh",
}
_GRIB_ALIASES = {name: name for name in VARIABLES} | _GRIB_EXTRAS

# Kelvin fields `to_standard_units` does not treat as air temperature.
_KELVIN_TEMPS = frozenset({"sst", "skt", "d2m", "mx2t6", "mn2t6", "st20", "st100", "mswpt300", "t"})


def _canonical_name(token: str) -> str | None:
    if token in VARIABLES:
        return token
    extra = _GRIB_EXTRAS.get(token)
    if extra is not None:
        return extra
    for short, spec in VARIABLES.items():
        if token == spec.ecds:
            return short
    return None


def _flatten_variable_tokens(raw) -> list[str]:
    """Accept ``-v tp t2m``, ``-v tp -v t2m``, and ``-v tp,t2m`` alike."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    tokens: list[str] = []
    for item in items:
        parts = item if isinstance(item, (list, tuple)) else [item]
        for part in parts:
            for token in str(part).replace(",", " ").split():
                if token:
                    tokens.append(token)
    return tokens


def _resolve_variables(raw: list | None) -> list[str]:
    tokens = _flatten_variable_tokens(raw) or list(DEFAULT_VARIABLES)
    unknown = [token for token in tokens if _canonical_name(token) is None]
    if unknown:
        raise UsageError(
            f"unknown variable(s): {', '.join(unknown)}.\n"
            f"Available (most used first): {', '.join(VARIABLES)}"
        )
    resolved: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        name = _canonical_name(token)
        if name not in seen:
            seen.add(name)
            resolved.append(name)
    return resolved


def _catalog_covers(init: dt.date, names: list[str]) -> bool:
    if init < _CATALOG_START:
        return False
    return all(name in _CATALOG_SURFACE or name in _CATALOG_PRESSURE for name in names)


def _use_staging_catalog() -> None:
    import dynamical_catalog

    os.environ["DYNAMICAL_STAC_CATALOG_URL"] = _STAGING_CATALOG
    dynamical_catalog.clear_cache()


def _ensure_catalog() -> None:
    """Prefer production; fall through to staging until the 46-day product is promoted."""
    import dynamical_catalog

    if _DATASET in dynamical_catalog.list():
        return
    _use_staging_catalog()
    ids = dynamical_catalog.list()
    if _DATASET not in ids:
        raise DataError(
            f"{_DATASET} is not in the dynamical.org catalog "
            f"(tried production and staging). Available: {', '.join(ids)}"
        )


def _open_catalog(group: str | None = None):
    import dynamical_catalog

    _ensure_catalog()
    kwargs: dict = {"chunks": None}
    if group:
        kwargs["group"] = group
    return dynamical_catalog.open(_DATASET, **kwargs)


def _catalog_latest_init() -> dt.date | None:
    import numpy as np

    try:
        ds = _open_catalog()
    except Exception:
        return None
    if ds.sizes.get("init_time", 0) == 0:
        return None
    return np_to_date(np.max(ds["init_time"].values))


def _drop_empty_lead0(ds):
    """Drop lead 0 when every requested field is all-NaN there, then reindex steps.

    Catalog 24-hour surface statistics are NaN at init (no preceding day). ECDS
    deaccumulation already writes `step=0` as the first 24 h, so shift remaining
    leads down to match. Instant fields (winds, pressure, pressure-level) keep
    lead 0.
    """
    import numpy as np

    if "lead_time" not in ds.dims or ds.sizes["lead_time"] < 2:
        return ds
    first = ds.isel(lead_time=0)
    for name in ds.data_vars:
        if not bool(np.asarray(first[name].isnull().all())):
            return ds
    ds = ds.isel(lead_time=slice(1, None))
    steps = np.arange(ds.sizes["lead_time"]) * np.timedelta64(1, "D")
    return ds.assign_coords(lead_time=("lead_time", steps))


def _standardize_catalog(ds):
    """CF attrs + standard units. Catalog precip is already a rate."""
    stamp_cf_attrs(ds)
    if "tp" in ds.data_vars:
        ds["tp"].attrs.setdefault("standard_name", "precipitation_flux")
        ds["tp"].attrs.setdefault("long_name", "Total precipitation")
    ds = ensure_normalized_longitude(ds)
    out = ds
    for name in list(ds.data_vars):
        try:
            out = to_standard_units(out, variables=[name])
        except UsageError:
            continue
    for name in _KELVIN_TEMPS:
        if name in out.data_vars:
            out[name] = _to_celsius(out[name])
    return stamp_data_interval(out)


def _fetch_catalog(init: dt.date, bbox, names: list[str]):
    import numpy as np
    import xarray as xr

    date_iso = init.isoformat()
    surface = [name for name in names if name in _CATALOG_SURFACE]
    pressure = [name for name in names if name in _CATALOG_PRESSURE]

    pieces = []
    if surface:
        ds = _open_catalog()[[_CATALOG_SURFACE[name] for name in surface]]
        pieces.append(ds)
    if pressure:
        pds = _open_catalog(group=_PRESSURE_GROUP)
        pds = pds[[_CATALOG_PRESSURE[name] for name in pressure]]
        if "pressure_level" in pds.dims:
            pds = pds.rename({"pressure_level": "vertical"})
            pds["vertical"].attrs.update(
                units="hPa",
                standard_name="air_pressure",
                long_name="pressure",
                positive="down",
                axis="Z",
            )
        pieces.append(pds)
    ds = pieces[0] if len(pieces) == 1 else xr.merge(pieces, compat="override")

    ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")

    inits = ds["init_time"].values
    init_target = np.datetime64(f"{date_iso}T00:00:00").astype(inits.dtype)

    def _no_init() -> DataError:
        return DataError(
            f"{_DATASET} has no {date_iso} 00 UTC init; available init range is "
            f"{np_to_date(inits.min()).isoformat()}..{np_to_date(inits.max()).isoformat()}."
        )

    if init_target not in inits:
        raise _no_init()
    try:
        ds = ds.sel(init_time=init_target)
    except KeyError:
        raise _no_init() from None

    drop = [c for c in _DROP_COORDS if c in ds.coords or c in ds]
    if drop:
        ds = ds.drop_vars(drop)
    ds = _drop_empty_lead0(ds)
    rename = {"lead_time": "step"}
    if "ensemble_member" in ds.dims:
        rename["ensemble_member"] = "number"
    ds = ds.rename(rename)
    if "init_time" in ds.coords or "init_time" in ds:
        ds = ds.assign_coords(time=ds["init_time"]).drop_vars("init_time")

    rename_vars = {
        name: _CATALOG_TO_SHORT[name]
        for name in ds.data_vars
        if name in _CATALOG_TO_SHORT and _CATALOG_TO_SHORT[name] != name
    }
    if rename_vars:
        ds = ds.rename(rename_vars)
    missing = [name for name in names if name not in ds.data_vars]
    if missing:
        have = ", ".join(ds.data_vars) if list(ds.data_vars) else "none"
        raise DataError(f"{_DATASET} did not contain {', '.join(missing)} (opened: {have}).")
    ds = ds[names]
    ds.attrs.update(Conventions="CF-1.13", weather_skills_source="ecmwf-s2s")
    return _standardize_catalog(ds)


def _request_group_key(name: str) -> tuple:
    spec = VARIABLES[name]
    return (spec.family, spec.level_type, spec.levels, spec.control_only)


def _group_for_request(names: list[str]) -> list[tuple[tuple, list[str]]]:
    """Split variables so each ECDS request has one leadtime family and level set."""
    groups: dict[tuple, list[str]] = {}
    for name in names:
        groups.setdefault(_request_group_key(name), []).append(name)
    return list(groups.items())


def _group_slug(key: tuple) -> str:
    family, level_type, levels, control_only = key
    nlev = f"{len(levels)}lev" if levels else "sfc"
    extra = "cfonly" if control_only else "ens"
    return f"{family}_{level_type}_{nlev}_{extra}"


def _daily_leadtimes(hours: list[str]) -> list[str]:
    """Map instant lead hours onto ECDS daily-averaged `start_end` windows."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in hours:
        end = int(raw)
        token = "0_24" if end == 0 else f"{end - 24}_{end}"
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _submit(client, request: dict):
    """Submit an s2s-forecasts retrieval; surface a clean message on licence-not-accepted."""
    import requests

    try:
        return client.submit("s2s-forecasts", request)
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        if (
            resp is not None
            and resp.status_code == 403
            and "licences not accepted" in str(e).lower()
        ):
            raise DataError(
                "ERROR: ECDS retrieval blocked: required licences not accepted on s2s-forecasts.\n"
                f"Action: open {S2S_LICENCE_URL} in a browser, log in to ECDS, "
                "accept the required licences, then re-run this skill.",
                prefix=False,
            ) from None
        raise


def _build_request(
    date_iso: str,
    area: list[float],
    forecast_type: str,
    variables: list[str] | tuple[str, ...] = ("tp",),
) -> dict:
    names = list(variables)
    families = {VARIABLES[name].family for name in names}
    if len(families) != 1:
        raise ValueError("internal: one leadtime family per ECDS request")
    family = next(iter(families))
    leadtimes = LEADTIME_HOURS if family == "instant" else _daily_leadtimes(LEADTIME_HOURS)
    level_types = {VARIABLES[name].level_type for name in names}
    if len(level_types) != 1:
        raise ValueError("internal: one level_type per ECDS request")
    level_sets = {VARIABLES[name].levels for name in names}
    if len(level_sets) != 1:
        raise ValueError("internal: one level list per ECDS request")
    spec0 = VARIABLES[names[0]]
    d = dt.date.fromisoformat(date_iso)
    req = {
        "origin": "ecmwf",
        "level_type": spec0.level_type,
        "variable": [VARIABLES[name].ecds for name in names],
        "year": [str(d.year)],
        "month": [f"{d.month:02d}"],
        "day": [f"{d.day:02d}"],
        "time": ["00:00"],
        "leadtime_hour": leadtimes,
        "forecast_type": forecast_type,
        "area": area,
        "data_format": "grib",
    }
    if spec0.levels:
        req[spec0.level_key] = list(spec0.levels)
    return req


def _split_wrapped_area(area: list[float]) -> list[list[float]]:
    """Split antimeridian-crossing [N, W, S, E] (W > E) into two MARS-valid areas."""
    n, w, s, e = area
    if w <= e:
        return [area]
    return [[n, w, s, 180.0], [n, -180.0, s, e]]


def _concat_lon(datasets: list) -> object:
    """Concatenate per-area datasets along longitude; drop duplicated +-180 seam."""
    import numpy as np
    import xarray as xr

    if len(datasets) == 1:
        return datasets[0]
    lon_name = None
    for cand in ("longitude", "lon", "x"):
        if cand in datasets[0].coords or cand in datasets[0].dims:
            lon_name = cand
            break
    if lon_name is None:
        return datasets[0]
    normed = [ensure_normalized_longitude(d, lon_dim=lon_name) for d in datasets]
    combined = xr.concat(normed, dim=lon_name)
    _, unique_idx = np.unique(combined[lon_name].values, return_index=True)
    return combined.isel({lon_name: np.sort(unique_idx)}).sortby(lon_name)


def _drop_grib_filters(ds):
    drop = [name for name in ds.coords if name not in ds.dims and name not in _KEEP_COORDS]
    return ds.drop_vars(drop) if drop else ds


def _open_grib(path: Path):
    """Decode a (possibly mixed-parameter) GRIB into one Dataset."""
    import cfgrib
    import xarray as xr

    parts = cfgrib.open_datasets(str(path))
    if not parts:
        raise DataError(f"ECDS GRIB {path.name} contained no messages.")
    cleaned = [_drop_grib_filters(part) for part in parts]
    if len(cleaned) == 1:
        return cleaned[0]
    return xr.merge(cleaned, compat="override", combine_attrs="override")


def _promote_vertical(ds):
    """Rename cfgrib pressure/theta coords onto the ontology ``vertical`` dim."""
    if "vertical" in ds.dims:
        return ds
    for name, units, standard_name in (
        ("isobaricInhPa", "hPa", "air_pressure"),
        ("isobaricInPa", "Pa", "air_pressure"),
        ("theta", "K", "air_potential_temperature"),
        ("level", None, None),
    ):
        if name not in ds.coords and name not in ds.dims:
            continue
        if name not in ds.dims:
            ds = ds.expand_dims(name)
        ds = ds.rename({name: "vertical"})
        if units:
            ds["vertical"].attrs.setdefault("units", units)
        if standard_name:
            ds["vertical"].attrs.setdefault("standard_name", standard_name)
            ds["vertical"].attrs.setdefault("positive", "down")
        ds["vertical"].attrs.setdefault("axis", "Z")
        return ds
    return ds


def _rename_to_short(ds, requested: list[str]):
    """Map cfgrib names onto canonical `-v` tokens and keep only those fields."""
    rename = {}
    for name in ds.data_vars:
        short = _GRIB_ALIASES.get(name)
        if short is not None and short in requested and short != name:
            rename[name] = short
    if rename:
        ds = ds.rename(rename)
    extra = [name for name in ds.data_vars if name not in requested]
    if extra:
        ds = ds.drop_vars(extra)
    missing = [name for name in requested if name not in ds.data_vars]
    if missing:
        have = ", ".join(ds.data_vars) if list(ds.data_vars) else "none"
        raise DataError(f"ECDS GRIB did not contain {', '.join(missing)} (decoded: {have}).")
    return ds[requested]


def _to_celsius(da):
    units = str(da.attrs.get("units") or "")
    if units in {"degree_Celsius", "degC", "celsius"}:
        return da
    converted, _ = convert_dataarray(da, "degree_Celsius")
    converted.attrs["units"] = "degree_Celsius"
    return converted


def _standardize(ds):
    """CF attrs + standard units. S2S ``tp`` is cumulative; convert to a rate."""
    stamp_cf_attrs(ds)
    if "tp" in ds.data_vars:
        ds["tp"].attrs["standard_name"] = "precipitation_amount"
        ds["tp"].attrs["units"] = "kg m-2"
        ds["tp"].attrs["long_name"] = "Total precipitation"
    ds = ensure_normalized_longitude(ds)
    ds = to_standard_units(ds)
    for name in _KELVIN_TEMPS:
        if name in ds.data_vars:
            ds[name] = _to_celsius(ds[name])
    ds = precip_amounts_to_rates(ds)
    return stamp_data_interval(ds)


def _is_s2s_embargo_error(exc: BaseException) -> bool:
    """True if `exc` (or its cause/context chain) matches the S2S real-time embargo."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < _EMBARGO_CHAIN_MAX_DEPTH:
        seen.add(id(cur))
        parts.append(str(cur))
        parts.append(type(cur).__name__)
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    haystack = " ".join(parts).lower()
    return any(sig in haystack for sig in _S2S_EMBARGO_SIGNATURES)


@weather_skill(
    name="ecmwf-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--bbox", required=True)
@weather_skill.argument("--date", required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    nargs="+",
    help=(
        "S2S fields to retrieve. Pass several names in one flag (`-v tp t2m`), "
        "repeat `-v`, or comma-separate (`-v tp,t2m`). Most used first: tp, "
        "t2m, sst. Pressure-level: gh, t, u, v, w, q. Default tp. Names are "
        "cfgrib short names (sst, t, not ARCO 2m_temperature)."
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
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(bbox, date, variable, **kwargs):
    """Fetch ECMWF S2S ensemble fields (cf + pf) and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        found = _catalog_latest_init()
        if found is not None:
            print(found.isoformat())
            return
        # Calendar fallback when the catalog is unreachable. 2-day embargo;
        # Mon/Thu only before 2023-06-27.
        day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
        if day < dt.date(2023, 6, 27):
            while day.weekday() not in (0, 3):
                day -= dt.timedelta(days=1)
        print(day.isoformat())
        return

    date_iso = date.isoformat()
    area = list(bbox)
    names = _resolve_variables(variable)

    if _catalog_covers(date, names):
        print(
            f"Fetching ECMWF S2S from dynamical:{_DATASET} "
            f"for area={area} date={date_iso} variables={','.join(names)}",
            file=sys.stderr,
        )
        return _fetch_catalog(date, bbox, names)

    require_env("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")

    import xarray as xr
    from ecmwf.datastores import Client
    from ecmwf.datastores.processing import ProcessingFailedError

    print(
        f"Fetching ECMWF S2S from ECDS for area={area} date={date_iso} variables={','.join(names)}",
        file=sys.stderr,
    )
    with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        sub_areas = _split_wrapped_area(area)
        client = Client()
        groups = _group_for_request(names)

        legs = []
        for key, group_vars in groups:
            slug = _group_slug(key)
            control_only = key[3]
            forecast_types = [("control_forecast", "cf")]
            if not control_only:
                forecast_types.append(("perturbed_forecast", "pf"))
            for forecast_type, short in forecast_types:
                for i, sub in enumerate(sub_areas):
                    legs.append(
                        {
                            "key": key,
                            "group_vars": group_vars,
                            "forecast_type": forecast_type,
                            "short": short,
                            "area": sub,
                            "grib": tmp / f"{short}_{slug}_{i}.grib",
                            "remote": None,
                        }
                    )

        print(f"Submitting {len(legs)} retrieval leg(s)...", file=sys.stderr)
        try:
            for leg in legs:
                req = _build_request(date_iso, leg["area"], leg["forecast_type"], leg["group_vars"])
                leg["remote"] = _submit(client, req)
        except DataError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"ECDS submit failed for init {date_iso} ({exc}); this is a transport/auth problem."
            ) from None

        remotes = [leg["remote"] for leg in legs]
        waited = 0
        while True:
            try:
                if all(r.results_ready for r in remotes):
                    break
            except ProcessingFailedError as exc:
                if _is_s2s_embargo_error(exc):
                    raise DataError(
                        f"init {date_iso} is inside the 2-day S2S real-time embargo "
                        f"(access-restricted) ({exc}); use an older init date."
                    ) from None
                raise DataError(
                    f"ECDS reported no data for init {date_iso} ({exc}); "
                    "there may be no published S2S real-time forecast for that date "
                    "(daily since 2023-06-27; Mondays/Thursdays only before then), "
                    "or the init is not yet available — try another date."
                ) from None
            except Exception as exc:  # noqa: BLE001
                if _is_s2s_embargo_error(exc):
                    raise DataError(
                        f"init {date_iso} is inside the 2-day S2S real-time embargo "
                        f"(access-restricted) ({exc}); use an older init date."
                    ) from None
                raise DataError(
                    f"polling ECDS job for init {date_iso} failed ({exc}); "
                    "this is a transport/auth problem."
                ) from None
            if waited >= _PROBE_POLL_MAX_SECONDS:
                raise DataError(
                    f"ECDS job for init {date_iso} was still not ready after "
                    f"{_PROBE_POLL_MAX_SECONDS}s; the job is stuck. Re-run, or pass a "
                    "different init date."
                )
            time.sleep(_PROBE_POLL_SECONDS)
            waited += _PROBE_POLL_SECONDS

        for leg in legs:
            print(f"Downloading {leg['grib'].name}...", file=sys.stderr)
            leg["remote"].download(str(leg["grib"]))

        print("Decoding GRIB and writing Zarr...", file=sys.stderr)
        family_ensembles = []
        for key, group_vars in groups:
            cf_parts = [
                _open_grib(leg["grib"])
                for leg in legs
                if leg["key"] == key and leg["forecast_type"] == "control_forecast"
            ]
            pf_parts = [
                _open_grib(leg["grib"])
                for leg in legs
                if leg["key"] == key and leg["forecast_type"] == "perturbed_forecast"
            ]
            cf = _promote_vertical(_concat_lon(cf_parts).assign_coords(number=0))
            if pf_parts:
                pf = _promote_vertical(_concat_lon(pf_parts))
                ens = xr.concat([pf, cf], dim="number").sortby("number")
            else:
                ens = cf
            family_ensembles.append(_rename_to_short(ens, group_vars))
        ds = (
            family_ensembles[0]
            if len(family_ensembles) == 1
            else xr.merge(family_ensembles, join="outer", compat="override")
        )
        ds = ds[names]
        ds.attrs.update(Conventions="CF-1.13", weather_skills_source="ecmwf-s2s")
        ds = _standardize(ds)
        # Materialize while GRIB files in the temp dir still exist.
        ds = ds.load()

    return ds


if __name__ == "__main__":
    fetch()

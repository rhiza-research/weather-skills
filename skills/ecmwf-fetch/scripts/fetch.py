# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "dynamical-catalog==1.0.1",
#   "ecmwf-datastores-client==0.4.2",
#   # >=0.3.34 for the IFS Cycle 50r1 (2026-05-12) date-aware oper/scda
#   # stream fix in Client.patch_stream — older pins misroute 06/18Z runs.
#   "ecmwf-opendata==0.3.34",
#   "requests",
#   "pandas",
#   "cfgrib",
#   # eccodeslib carries the native libeccodes that cfgrib loads on macOS/Linux;
#   # eccodes drops this as a transitive dep via PyPI metadata (ecmwf/eccodes-python#150),
#   # so it is declared directly. Windows bundles the library in eccodes itself.
#   "eccodeslib",
#   "gcsfs",
# ]
# ///
"""Fetch an ECMWF forecast product (S2S, IFS-ENS, AIFS, HRES) as a weather-skills Zarr."""

from __future__ import annotations

import datetime as dt
import os
import re
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
_SKILL_VERSION = "0.0.9"

# Product -> dynamical.org dataset id (hres is not on dynamical.org).
PRODUCTS = {
    "s2s": "ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree",
    "s2s-6h": "ecmwf-ifs-ens-forecast-46-day-6-hourly-1-5-degree",
    "ifs-ens": "ecmwf-ifs-ens-forecast-15-day-0-25-degree",
    "aifs-ens": "ecmwf-aifs-ens-forecast",
    "aifs-single": "ecmwf-aifs-single-forecast",
    "hres": None,
}

_PROBE_POLL_SECONDS = 30
_PROBE_POLL_MAX_SECONDS = 3600

# S2S archives precipitation 6-hourly; request 00Z daily leads through the 46-day S2S range.
_MAX_LEAD_HOURS = 46 * 24
LEADTIME_HOURS = [str(h) for h in range(0, _MAX_LEAD_HOURS + 1, 24)]

S2S_LICENCE_URL = "https://ecds.ecmwf.int/datasets/s2s-forecasts?tab=download#manage-licences"

# The ECDS API base URL is a fixed, public endpoint, not a secret — set it
# ourselves so only ECMWF_DATASTORES_KEY needs to be injected. An explicit
# override in the environment (e.g. a staging endpoint) still wins.
_ECDS_URL = "https://ecds.ecmwf.int/api"


def _ensure_datastores_url() -> None:
    os.environ.setdefault("ECMWF_DATASTORES_URL", _ECDS_URL)


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
# tokens are cfgrib short names (sst, not ARCO 2m_temperature), except
# rainfall, written as `precip`.
VARIABLES: dict[str, _Var] = {
    "precip": _Var("total_precipitation", "instant"),
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
DEFAULT_VARIABLES = ["precip"]

# --- HRES (ECMWF Open Data) --------------------------------------------------

_FULL_RUN_STEPS = list(range(0, 144 + 1, 3)) + list(range(150, 360 + 1, 6))
_SHORT_CUTOFF_STEPS = list(range(0, 144 + 1, 3))

# data.ecmwf.int keeps only the inits of the last 4 days (today included);
# older inits are only on the AWS mirror (from 2023).
_ECMWF_DAYS_KEPT = 4

# Every Open Data level from 1000 to 50 hPa. 10 hPa exists only since IFS
# Cycle 50r1 (2026-05-12), so it is not requested: older inits would fail.
HRES_PRESSURE_LEVELS = (
    "1000",
    "925",
    "850",
    "700",
    "600",
    "500",
    "400",
    "300",
    "250",
    "200",
    "150",
    "100",
    "50",
)


class _HresVar(NamedTuple):
    param: str  # ECMWF Open Data short param name
    level_type: str = "sfc"  # "sfc" | "pl"
    levels: tuple[str, ...] = ()


# Most-used first, then the rest of the HRES ("oper") single-level and
# pressure-level fields carried by ECMWF Open Data.
HRES_VARIABLES: dict[str, _HresVar] = {
    "precip": _HresVar("tp"),
    "t2m": _HresVar("2t"),
    "d2m": _HresVar("2d"),
    "msl": _HresVar("msl"),
    "u10": _HresVar("10u"),
    "v10": _HresVar("10v"),
    "gust10": _HresVar("10fg"),
    "sp": _HresVar("sp"),
    "tcwv": _HresVar("tcwv"),
    # Open Data publishes most-unstable CAPE only; there is no `cape` field.
    "mucape": _HresVar("mucape"),
    "skt": _HresVar("skt"),
    "ssrd": _HresVar("ssrd"),
    "strd": _HresVar("strd"),
    "str": _HresVar("str"),
    "ttr": _HresVar("ttr"),
    "t": _HresVar("t", "pl", HRES_PRESSURE_LEVELS),
    "gh": _HresVar("gh", "pl", HRES_PRESSURE_LEVELS),
    "u": _HresVar("u", "pl", HRES_PRESSURE_LEVELS),
    "v": _HresVar("v", "pl", HRES_PRESSURE_LEVELS),
    "q": _HresVar("q", "pl", HRES_PRESSURE_LEVELS),
    "r": _HresVar("r", "pl", HRES_PRESSURE_LEVELS),
}

# --- dynamical.org catalog ---------------------------------------------------

# 46-day inits on dynamical.org start 2026-01-01 (still ~2-day embargo).
_CATALOG_START = dt.date(2026, 1, 1)
_STAGING_CATALOG = "https://stac-staging.dynamical.org/catalog.json"
_PRESSURE_GROUP = "pressure_level"
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)
# Medium-range stores keep selected levels as separate `temperature_850hpa` fields.
_HPA_RE = re.compile(r"^(.+)_(\d+)hpa$")

# Short name -> catalog fields it stands for, first match wins. Fluxes (ECDS
# accumulated J m-2 vs catalog mean W m-2), 6-hour max/min, static masks,
# ocean, and pv are not mapped, so S2S takes them from ECDS. Unmapped catalog
# fields keep their catalog name.
_CATALOG_NAMES = {
    "precip": ("precipitation_surface",),
    "t2m": ("temperature_2m", "average_temperature_2m"),
    "d2m": ("dew_point_temperature_2m", "average_dew_point_temperature_2m"),
    "sst": ("sea_surface_temperature",),
    "u10": ("wind_u_10m",),
    "v10": ("wind_v_10m",),
    "u100": ("wind_u_100m",),
    "v100": ("wind_v_100m",),
    "gust10": ("wind_gust_10m",),
    "msl": ("pressure_reduced_to_mean_sea_level",),
    "sp": ("pressure_surface",),
    "cape": ("average_convective_available_potential_energy_surface",),
    "tcw": ("total_column_water_atmosphere",),
    "skt": ("skin_temperature_surface",),
    "tcc": ("total_cloud_cover_atmosphere",),
    "sd": ("snow_water_equivalent_surface",),
    "rsn": ("snow_density_surface",),
    "asn": ("snow_albedo_surface",),
    "sm20": ("soil_moisture_0_20cm",),
    "sm100": ("soil_moisture_0_100cm",),
    "st20": ("soil_temperature_0_20cm",),
    "st100": ("soil_temperature_0_100cm",),
    "ci": ("sea_ice_area_fraction",),
    "cp": ("precipitation_convective_surface",),
    "sf": ("snowfall_water_equivalent_rate_surface",),
    "ewss": ("eastward_turbulent_surface_stress",),
    "nsss": ("northward_turbulent_surface_stress",),
    "ro": ("runoff_water_equivalent_surface",),
    "sro": ("runoff_surface",),
    "gh": ("geopotential_height",),
    "t": ("temperature",),
    "u": ("wind_u",),
    "v": ("wind_v",),
    "w": ("vertical_velocity",),
    "q": ("specific_humidity",),
}
_CATALOG_TO_SHORT = {cat: short for short, cats in _CATALOG_NAMES.items() for cat in cats}

# cfgrib / S2S / Open Data abbreviations and other names accepted for a canonical `-v` token.
_GRIB_EXTRAS = {
    "tp": "precip",
    "precipitation": "precip",
    "2t": "t2m",
    "2d": "d2m",
    "10u": "u10",
    "10v": "v10",
    "10fg": "gust10",
    "wtmp": "sst",
    "z": "gh",
}
_GRIB_ALIASES = {name: name for name in VARIABLES | HRES_VARIABLES} | _GRIB_EXTRAS

# Cumulative-since-init fields besides `precip`, written as-is (not deaccumulated).
# Mixing them with `precip` would relabel their steps onto precip's shortened axis.
_ACCUMULATED = frozenset(
    {"cp", "sf", "ro", "sro", "sshf", "slhf", "ssr", "ssrd", "str", "strd", "ttr", "ewss", "nsss"}
)

# Kelvin fields `to_standard_units` does not treat as air temperature.
_KELVIN_TEMPS = frozenset(
    {"t2m", "sst", "skt", "d2m", "mx2t6", "mn2t6", "st20", "st100", "mswpt300", "t"}
)


def _canonical_name(token: str, table: dict = VARIABLES) -> str | None:
    if token in table:
        return token
    extra = _GRIB_EXTRAS.get(token)
    if extra in table:
        return extra
    for short, spec in table.items():
        if token == spec[0]:
            return short
    return None


def _flatten_variable_tokens(raw) -> list[str]:
    """Accept ``-v precip t2m``, ``-v precip -v t2m``, and ``-v precip,t2m`` alike."""
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


def _resolve_variables(raw: list | None, table: dict = VARIABLES) -> list[str]:
    tokens = _flatten_variable_tokens(raw) or list(DEFAULT_VARIABLES)
    unknown = [token for token in tokens if _canonical_name(token, table) is None]
    if unknown:
        raise UsageError(
            f"unknown variable(s): {', '.join(unknown)}.\n"
            f"Available (most used first): {', '.join(table)}"
        )
    resolved: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        name = _canonical_name(token, table)
        if name not in seen:
            seen.add(name)
            resolved.append(name)
    return resolved


def _catalog_covers(init: dt.date, tokens: list[str]) -> bool:
    """True when the S2S catalog can serve the init and no token is an ECDS-only field."""
    if init < _CATALOG_START:
        return False
    return all(_canonical_name(t) in (None, *_CATALOG_NAMES) for t in tokens)


def _open_catalog(dataset: str, group: str | None = None):
    """Open a dataset (production catalog, else staging until it is promoted)."""
    import dynamical_catalog

    try:
        if dataset not in dynamical_catalog.list():
            os.environ["DYNAMICAL_STAC_CATALOG_URL"] = _STAGING_CATALOG
            dynamical_catalog.clear_cache()
            ids = dynamical_catalog.list()
            if dataset not in ids:
                raise DataError(
                    f"{dataset} is not in the dynamical.org catalog "
                    f"(tried production and staging). Available: {', '.join(ids)}"
                )
        kwargs: dict = {"chunks": None}
        if group:
            kwargs["group"] = group
        return dynamical_catalog.open(dataset, **kwargs)
    except DataError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"could not open {dataset} from the dynamical.org catalog ({exc})."
        ) from None


def _resolve_catalog(tokens: list[str], data_vars, dataset: str) -> list[str]:
    """Map `-v` tokens onto catalog fields: catalog names, short names, or a level prefix.

    A pressure-level token (`t`, `temperature`) takes every `*_Nhpa` field of
    that prefix (never `temperature_2m`), or the single grouped field.
    """
    hpa: dict[str, list[str]] = {}
    for name in data_vars:
        match = _HPA_RE.match(name)
        if match:
            hpa.setdefault(match.group(1), []).append(name)
    resolved: list[str] = []
    missing: list[str] = []
    for token in tokens:
        short = _GRIB_EXTRAS.get(token) or _canonical_name(token) or token
        found: list[str] = []
        for cand in (token, *_CATALOG_NAMES.get(short, ())):
            if cand in data_vars:
                found = [cand]
                break
            if cand in hpa:
                found = sorted(hpa[cand], key=lambda n: -int(_HPA_RE.match(n).group(2)))
                break
        if not found:
            missing.append(token)
        resolved += [name for name in found if name not in resolved]
    if missing:
        raise UsageError(
            f"variable(s) not in {dataset}: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(data_vars))}\n"
            "Run with --list-variables to see them with units and pressure levels."
        )
    return resolved


def _set_vertical_attrs(ds):
    ds["vertical"].attrs.update(
        units="hPa", standard_name="air_pressure", long_name="pressure", positive="down", axis="Z"
    )
    return ds


def _stack_pressure_levels(ds):
    """Combine `*_Nhpa` variables into one field with a `vertical` (hPa) dim."""
    import xarray as xr

    groups: dict[str, list[tuple[int, str]]] = {}
    for name in ds.data_vars:
        match = _HPA_RE.match(name)
        if match:
            groups.setdefault(match.group(1), []).append((int(match.group(2)), name))
    if not groups:
        return ds
    drop = [name for items in groups.values() for _, name in items]
    pieces = [ds.drop_vars(drop)]
    for prefix, items in groups.items():
        items = sorted(items, key=lambda it: -it[0])
        stacked = xr.concat(
            [ds[name].expand_dims(vertical=[float(hpa)]) for hpa, name in items], dim="vertical"
        )
        stacked.name = prefix
        pieces.append(_set_vertical_attrs(stacked.to_dataset()))
    return xr.merge(pieces)


def _list_catalog_variables(dataset: str) -> None:
    ds = _open_catalog(dataset)
    print(f"{dataset} variables (pass the name or its short name to -v):")
    for name in sorted(ds.data_vars):
        attrs = ds[name].attrs
        print(f"  {name}\t{attrs.get('units', '')}\t{attrs.get('long_name', '')}")
    try:
        pds = _open_catalog(dataset, group=_PRESSURE_GROUP)
    except DataError:  # most stores have no pressure-level group
        return
    levels = ", ".join(f"{float(v):g}" for v in pds["pressure_level"].values)
    print(f"{_PRESSURE_GROUP} group, levels {levels} hPa (-v NAME takes every level):")
    for name in sorted(pds.data_vars):
        attrs = pds[name].attrs
        print(f"  {name}\t{attrs.get('units', '')}\t{attrs.get('long_name', '')}")


def _fetch_catalog(dataset, tokens, bbox, date, windows, max_lead_h, source):
    """Read one product from dynamical.org and write it on the skill's output contract."""
    import numpy as np
    import xarray as xr

    ds = _open_catalog(dataset)
    try:
        names = _resolve_catalog(tokens, ds.data_vars, dataset)
    except UsageError as err:
        # 46-day fields on pressure levels live in a separate group.
        try:
            pds = _open_catalog(dataset, group=_PRESSURE_GROUP)
        except DataError:
            raise err from None
        if "pressure_level" in pds.dims:
            pds = _set_vertical_attrs(pds.rename({"pressure_level": "vertical"}))
        ds = xr.merge([ds, pds], compat="override")
        names = _resolve_catalog(tokens, ds.data_vars, dataset)
    ds = ds[names]

    inits = ds["init_time"].values
    init_range = f"{np_to_date(inits.min()).isoformat()}..{np_to_date(inits.max()).isoformat()}"
    when = date.isoformat() if date is not None else "selected"
    if max_lead_h is not None:
        limit = np.timedelta64(round(max_lead_h * 3600), "s")
        # One catalog lead past the limit: end-labelled fields move back one
        # step below, and the extra lead is trimmed after that.
        n = int((ds["lead_time"].values <= limit).sum())
        ds = ds.isel(lead_time=slice(0, n + 1))
    if date is not None:
        init_target = np.datetime64(f"{date.isoformat()}T00:00:00").astype(inits.dtype)
        if init_target not in inits:
            raise DataError(
                f"{dataset} has no {date.isoformat()} 00 UTC init; "
                f"available init range is {init_range}."
            )
        ds = ds.sel(init_time=init_target)
    else:
        # 00 UTC inits only, as with --date: the windows select init times.
        midnight = inits == inits.astype("datetime64[D]")
        keep = np.zeros(inits.size, dtype=bool)
        for start, end in windows:
            lo = np.datetime64(start.isoformat())
            hi = np.datetime64(end.isoformat()) + np.timedelta64(1, "D")
            keep |= midnight & (inits >= lo) & (inits < hi)
        if not keep.any():
            window_text = ", ".join(f"{start}..{end}" for start, end in windows)
            raise DataError(
                f"{dataset} has no 00 UTC init in {window_text}; "
                f"available init range is {init_range}."
            )
        ds = ds.isel(init_time=np.nonzero(keep)[0])
        print(f"Selected {int(keep.sum())} inits.", file=sys.stderr)
    ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])
    rename = {"lead_time": "step", "init_time": "time"}
    if "ensemble_member" in ds.dims:
        rename["ensemble_member"] = "number"
    ds = ds.rename(rename)

    # Spatial subset after the time/variable selection: an antimeridian bbox
    # concatenates two longitude wings, which reads every selected value.
    if bbox is not None:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
    else:
        ds = ensure_normalized_longitude(ds, lon_dim="longitude")
    # Read the subset once: the NaN checks below would otherwise re-read it.
    ds = ds.load()
    # Same order as the ECDS path, so a product's output doesn't depend on its source.
    ds = ds.transpose(*[d for d in ("time", "number", "step") if d in ds.dims], ...)

    empty = np.ones(ds.sizes["step"], dtype=bool)
    for name in ds.data_vars:
        other = [d for d in ds[name].dims if d != "step"]
        empty &= ds[name].isnull().all(other).values
    if empty.all():
        raise DataError(
            f"{dataset} {when} init has no data for the requested variables and "
            "bbox (still publishing?); try the previous day's init."
        )
    # A fresh init is still publishing its long leads (all-NaN). Drop that
    # tail and say so instead of writing empty leads.
    last = int(np.nonzero(~empty)[0][-1])
    if last < ds.sizes["step"] - 1:
        hours = ds["step"].values / np.timedelta64(1, "h")
        print(
            f"WARNING: {dataset} {when} init has no data after catalog lead "
            f"{hours[last]:g} h (of {hours[-1]:g} h; still publishing?). Dropped the "
            "empty leads (not padded); use the previous day's init for every lead.",
            file=sys.stderr,
        )
        ds = ds.isel(step=slice(0, last + 1))
    # A range of inits: the newest may still be publishing its long leads
    # while older inits fill them. Its empty leads stay NaN (not filled).
    if "time" in ds.dims:
        init_empty = np.ones((ds.sizes["time"], ds.sizes["step"]), dtype=bool)
        for name in ds.data_vars:
            other = [d for d in ds[name].dims if d not in ("time", "step")]
            init_empty &= ds[name].isnull().all(other).transpose("time", "step").values
        hours = ds["step"].values / np.timedelta64(1, "h")
        for i, row in enumerate(init_empty):
            init = str(ds["time"].values[i])[:13]
            if row.all():
                print(f"WARNING: {dataset} {init} UTC init has no data (NaN).", file=sys.stderr)
            elif row[-1]:
                print(
                    f"WARNING: {dataset} {init} UTC init has no data after catalog lead "
                    f"{hours[np.nonzero(~row)[0][-1]]:g} h (still publishing?); its later "
                    "leads are NaN (not filled).",
                    file=sys.stderr,
                )

    # Averaged fields (precip, daily means) are labelled at the END of their
    # interval, so lead 0 is all-NaN. When every field is NaN there, drop lead 0
    # and relabel each step to the previous lead: step = interval start.
    # Instant fields (winds, pressure levels, instant t2m) keep lead 0.
    leads = ds["step"].values
    first = ds.isel(step=0)
    if ds.sizes["step"] > 1 and all(bool(first[n].isnull().all()) for n in ds.data_vars):
        ds = ds.isel(step=slice(1, None)).assign_coords(step=leads[:-1])
        # IFS ENS goes 3- to 6-hourly. Write each cell's [start, end] here:
        # stamp_data_interval would infer bounds as if steps were ends.
        if len(set(np.diff(leads))) > 1:
            pairs = np.stack([leads[:-1], leads[1:]], axis=1)
            ds = ds.assign_coords(step_bounds=(("step", "nv"), pairs))
            ds["step"].attrs["bounds"] = "step_bounds"
        note = "Steps relabelled to interval start: step s covers [s, next lead)."
        for name in ds.data_vars:
            ds[name].attrs["comment"] = f"{ds[name].attrs.get('comment', '')} {note}".strip()
        print("Dropped all-NaN lead 0 and relabelled steps to interval start.", file=sys.stderr)
    elif ds.sizes["step"] > 1 and not any(bool(first[n].isnull().all()) for n in ds.data_vars):
        # Only instant fields: on uneven leads give each sample the cell
        # [s, next lead), so a day bin holds the samples labelled in that day.
        if len(set(np.diff(leads))) > 1:
            ends = np.append(leads[1:], leads[-1] + (leads[-1] - leads[-2]))
            ds = ds.assign_coords(step_bounds=(("step", "nv"), np.stack([leads, ends], axis=1)))
            ds["step"].attrs["bounds"] = "step_bounds"
    if max_lead_h is not None:
        ds = ds.sel(step=slice(None, limit))
        # Mixed with instant fields, an end-labelled field has no value at step 0.
        dropped = [name for name in ds.data_vars if bool(ds[name].isnull().all())]
        if dropped:
            print(
                f"WARNING: dropped {', '.join(dropped)}: no value up to --max-lead "
                f"{max_lead_h:g} h (averaged fields are labelled at the end of their interval).",
                file=sys.stderr,
            )
            ds = ds.drop_vars(dropped)

    ds = _stack_pressure_levels(ds)
    ds = ds.rename({n: _CATALOG_TO_SHORT[n] for n in ds.data_vars if n in _CATALOG_TO_SHORT})
    ds.attrs.update(Conventions="CF-1.13", weather_skills_source=source)
    stamp_cf_attrs(ds)
    if "precip" in ds.data_vars:
        ds["precip"].attrs.setdefault("standard_name", "precipitation_flux")
        ds["precip"].attrs.setdefault("long_name", "Total precipitation")
    # Convert each variable alone so one inconvertible field does not abort the fetch.
    for name in list(ds.data_vars):
        try:
            ds = to_standard_units(ds, variables=[name])
        except UsageError:
            continue
    for name in _KELVIN_TEMPS:
        if name in ds.data_vars:
            ds[name] = _to_celsius(ds[name])
    # Stamp the lead spacing: with an init window, `time` is the init axis
    # and its 1-day gaps are not the cell length.
    return stamp_data_interval(ds, dim="step")


# --- Sheerwater reforecast (hindcast) stores ------------------------------------

_HINDCAST_ROOT = "gs://sheerwater-public-datalake/hindcasts"
# Label -> (store file prefix, short names it has). Planette (IFS CY49R1, one model
# cycle, every second day 2006-2025) first; WeatherBench2 (mixed cycles, 1996-2021)
# serves only inits before Planette's first, so one window never mixes the two.
_HINDCAST_STORES = {
    "ecmwf_planette": ("ecmwf_planette", ("precip", "t2m", "sst")),
    "ecmwf_wb2": ("ecmwf_ifs", ("precip", "t2m")),
}
# Short name -> (variable in the store, units, standard_name). The stores' own unit
# strings ("mm" for a daily rate, "C") are not all right, so units are set here.
_HINDCAST_VARS = {
    "precip": ("precip", "mm day-1", "precipitation_flux"),
    "t2m": ("tmp2m", "degree_Celsius", "air_temperature"),
    "sst": ("sst", "degree_Celsius", "sea_surface_temperature"),
}


def _open_hindcast(path: str):
    """Open a public reforecast store lazily: (member, prediction_timedelta, lat, lon, init_time)."""
    import xarray as xr

    try:
        return xr.open_zarr(
            path, storage_options={"token": "anon"}, decode_timedelta=True, chunks=None
        )
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed to open {path} ({exc}).") from None


def _fetch_hindcast(names, bbox, date, windows, max_lead_h):
    """Read ECMWF reforecast inits from the Sheerwater stores on the live s2s output contract.

    Returns None when --date is not a reforecast init for every requested field.
    """
    import numpy as np
    import xarray as xr

    spans = [(date, date)] if date is not None else windows
    parts: dict[str, list] = {name: [] for name in names}
    taken = np.array([], dtype="datetime64[D]")
    first = np.datetime64("9999-12-31", "D")  # first init of the stores read so far
    used, ranges = [], {}
    wb2_days = np.array([], dtype="datetime64[D]")
    for label, (prefix, served) in _HINDCAST_STORES.items():
        if all(np.datetime64(start, "D") >= first for start, _ in spans):
            continue
        new = []
        store_first = first
        for name in [n for n in names if n in served]:
            var = _HINDCAST_VARS[name][0]
            path = f"{_HINDCAST_ROOT}/{prefix}_{var}.zarr"
            ds = _open_hindcast(path)[[var]].drop_vars("time", errors="ignore")
            day = ds["init_time"].values.astype("datetime64[D]")
            ranges.setdefault(label, f"{label} {day.min()}..{day.max()}")
            keep = np.any(
                [(day >= np.datetime64(s, "D")) & (day <= np.datetime64(e, "D")) for s, e in spans],
                axis=0,
            ) & (day < first)
            store_first = min(store_first, day.min())
            if not keep.any():
                continue
            new.append(day[keep])
            # Still lazy: only the chunks inside the inits, leads and bbox are read.
            ds = ds.isel(init_time=keep).rename(
                {
                    var: name,
                    "init_time": "time",
                    "member": "number",
                    "prediction_timedelta": "step",
                    "lat": "latitude",
                    "lon": "longitude",
                }
            )
            if max_lead_h is not None:
                ds = ds.sel(step=slice(None, np.timedelta64(round(max_lead_h * 3600), "s")))
            ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
            try:
                parts[name].append(ds.load())
            except Exception as exc:  # noqa: BLE001
                raise DataError(f"failed to read {path} ({exc}).") from None
        first = store_first
        if new:
            used.append(label)
            days = np.unique(np.concatenate(new))
            taken = np.union1d(taken, days)
            if label == "ecmwf_wb2":
                wb2_days = days

    if date is not None and any(not parts[name] for name in names):
        return None
    if not taken.size:
        window_text = ", ".join(f"{s}..{e}" for s, e in windows)
        raise DataError(
            f"no ECMWF reforecast init in {window_text}; the stores run {'; '.join(ranges.values())}."
        )
    absent = [name for name in names if not parts[name]]
    if absent:
        raise DataError(
            f"no ECMWF reforecast {', '.join(absent)} for the inits in the window "
            "(sst is in ecmwf_planette only, inits 2006-01-03..2025-06-01)."
        )

    source = "+".join(f"sheerwater-hindcast:{label}" for label in used)
    print(
        f"Fetching ECMWF s2s reforecast from {source}: {taken.size} init(s), 10 members, "
        f"variables={','.join(names)}",
        file=sys.stderr,
    )
    if wb2_days.size:
        print(
            f"WARNING: {wb2_days.size} init(s) ({wb2_days[0]}..{wb2_days[-1]}) come from the "
            "WeatherBench2 IFS reforecast, whose model cycle changes from init to init; a trend "
            "across them can be a model change.",
            file=sys.stderr,
        )
    times = taken.astype("datetime64[ns]")
    fields = []
    for name in names:
        da = xr.concat(parts[name], "time", join="exact")[name]
        missing = np.setdiff1d(taken, da["time"].values.astype("datetime64[D]"))
        if missing.size:
            print(
                f"WARNING: reforecast {name} has no run for {missing.size} init(s), left NaN: "
                + ", ".join(str(d) for d in missing),
                file=sys.stderr,
            )
        da = da.reindex(time=times)
        da.attrs = {"units": _HINDCAST_VARS[name][1], "standard_name": _HINDCAST_VARS[name][2]}
        fields.append(da)
    try:
        ds = xr.merge(fields, join="exact")
    except ValueError as exc:
        raise DataError(f"reforecast stores are on different grids ({exc}).") from None
    ds = ds.transpose("time", "number", "step", "latitude", "longitude")
    if date is not None:
        ds = ds.isel(time=0)
    ds.attrs = {"Conventions": "CF-1.13", "weather_skills_source": source}
    stamp_cf_attrs(ds)
    if "precip" in ds.data_vars:
        ds["precip"].attrs["long_name"] = "Total precipitation"
    # Step d is the day [init + d, init + d + 1).
    return stamp_data_interval(ds, period="1 day", dim="step")


# --- ECDS ---------------------------------------------------------------------


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
    variables: list[str] | tuple[str, ...] = ("precip",),
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


# --- HRES requests -------------------------------------------------------------


def _steps_for(run_hour: int, max_lead_h: float | None = None) -> list[int]:
    steps = _SHORT_CUTOFF_STEPS if run_hour in (6, 18) else _FULL_RUN_STEPS
    if max_lead_h is None:
        return steps
    return [s for s in steps if s <= max_lead_h]


def _hres_request(
    date_iso: str,
    run_hour: int,
    group_vars: list[str],
    level_type: str,
    levels: tuple[str, ...],
    steps: list[int],
) -> dict:
    req: dict = {
        "date": dt.date.fromisoformat(date_iso),
        "time": run_hour,
        # Deliberately omit `stream`. ecmwf-opendata's Client resolves it
        # itself (Client.patch_stream), and that resolution is date-aware:
        # IFS Cycle 50r1 (2026-05-12) moved 06/18Z runs from stream=scda to
        # stream=oper. Passing an explicit "oper" gets inconsistently
        # remapped back to "scda" by Client.retrieve()'s index-file path for
        # some 06/18Z requests — let the client infer it instead.
        "type": "fc",
        "step": steps,
        "levtype": level_type,
        "param": [HRES_VARIABLES[name].param for name in group_vars],
    }
    if levels:
        req["levelist"] = list(levels)
    return req


# --- shared ---------------------------------------------------------------------


def _parse_max_lead(value: str) -> float:
    """Hours from ``24``, ``24h`` or ``10D`` (bare numbers are hours)."""
    import pandas as pd

    text = value.strip()
    try:
        hours = float(text) if text.replace(".", "", 1).isdigit() else None
        if hours is None:
            hours = pd.Timedelta(text) / pd.Timedelta(hours=1)
    except ValueError:
        raise UsageError(f"--max-lead {value!r} is not a lead time (24, 24h, 10D)") from None
    if hours < 0:
        raise UsageError("--max-lead must not be negative")
    return hours


def _drop_grib_filters(ds):
    drop = [name for name in ds.coords if name not in ds.dims and name not in _KEEP_COORDS]
    return ds.drop_vars(drop) if drop else ds


def _open_grib(path: Path):
    """Decode a (possibly mixed-parameter) GRIB into one Dataset."""
    import cfgrib
    import xarray as xr

    parts = cfgrib.open_datasets(str(path))
    if not parts:
        raise DataError(f"GRIB {path.name} contained no messages.")
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
        raise DataError(f"GRIB did not contain {', '.join(missing)} (decoded: {have}).")
    return ds[requested]


def _to_celsius(da):
    units = str(da.attrs.get("units") or "")
    if units in {"degree_Celsius", "degC", "celsius"}:
        return da
    converted, _ = convert_dataarray(da, "degree_Celsius")
    converted.attrs["units"] = "degree_Celsius"
    return converted


def _standardize(ds):
    """CF attrs + standard units for GRIB fields. Cumulative ``precip`` becomes a rate."""
    stamp_cf_attrs(ds)
    if "precip" in ds.data_vars:
        ds["precip"].attrs["long_name"] = "Total precipitation"
    ds = ensure_normalized_longitude(ds)
    ds = to_standard_units(ds)
    for name in _KELVIN_TEMPS:
        if name in ds.data_vars:
            ds[name] = _to_celsius(ds[name])
    ds = precip_amounts_to_rates(ds)
    return stamp_data_interval(ds)


@weather_skill(
    name="ecmwf-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--product",
    default="s2s",
    choices=tuple(PRODUCTS),
    help=(
        "s2s (46-day daily ensemble, default), s2s-6h (46-day 6-hourly), ifs-ens "
        "(15-day 0.25° ensemble), aifs-ens, aifs-single, hres (deterministic, Open Data)."
    ),
)
@weather_skill.argument("--bbox", help="N/W/S/E. Required for s2s and hres.")
@weather_skill.argument("--date", help="Init date (00 UTC; with hres, see --run).")
@weather_skill.argument(
    "--start-time",
    action="append",
    help="Init window start; repeat with --end-time for several windows. Not for hres.",
)
@weather_skill.argument("--end-time", action="append", help="Window end, paired with --start-time.")
@weather_skill.argument(
    "--run",
    type=int,
    choices=(0, 6, 12, 18),
    help=(
        "hres only: init hour (UTC). 00/12 publish the full 15-day range (3-hourly to "
        "144h, 6-hourly to 360h); 06/18 (short cutoff) only to 144h, "
        "3-hourly throughout. Default 0."
    ),
)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    nargs="+",
    help=(
        "Fields to retrieve. Pass several names in one flag (`-v precip t2m`), "
        "repeat `-v`, or comma-separate (`-v precip,t2m`). Short names: precip "
        "(also tp), t2m, sst, d2m, u10, v10, msl; pressure-level gh, t, u, v, w, q. "
        "Default precip. dynamical.org products also take catalog names."
    ),
)
@weather_skill.argument(
    "--max-lead",
    help=(
        "Keep steps up to this lead (24, 24h, 10D; bare numbers are hours); 0 is the "
        "initial step only. Default: every step."
    ),
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest init date (YYYY-MM-DD, usable as --date) of --product on "
        "stdout and exit. Does not download fields. IDENT is ignored."
    ),
)
@weather_skill.argument(
    "--list-variables",
    action="store_true",
    probe=True,
    help=(
        "dynamical.org products: print the catalog variables (name, units, long name; "
        "pressure levels for the pressure-level group) and exit."
    ),
)
def fetch(
    product,
    bbox,
    date,
    start_time,
    end_time,
    run,
    variable,
    max_lead,
    probe_latest,
    list_variables,
    **kwargs,
):
    """Fetch an ECMWF forecast product (S2S, IFS-ENS, AIFS, HRES) as a weather-skills Zarr."""
    dataset = PRODUCTS[product]
    if run is not None and product != "hres":
        raise UsageError("--run is for --product hres only.")
    if list_variables:
        if dataset is None:
            raise UsageError("--list-variables is for the dynamical.org products, not hres.")
        _list_catalog_variables(dataset)
        if product == "s2s":
            fields = ", ".join(n for n in VARIABLES if n not in _CATALOG_NAMES)
            print(f"ECMWF Data Store fields (any init; needs ECMWF_DATASTORES_KEY): {fields}")
        return

    if probe_latest is not None:
        if product == "hres":
            from ecmwf.opendata import Client

            client = Client(source="ecmwf")
            try:
                latest = client.latest(stream="oper", type="fc", step=0, levtype="sfc", param="2t")
            except Exception as exc:  # noqa: BLE001
                raise DataError(f"could not determine the latest HRES init ({exc}).") from None
            # stdout is a bare date so it passes straight to --date (whose 00 UTC
            # run is published whenever any run that day is); stderr names the run.
            print(
                f"Latest run: {latest:%Y-%m-%d} {latest:%H} UTC "
                f"(--date {latest:%Y-%m-%d} --run {latest.hour})",
                file=sys.stderr,
            )
            print(latest.strftime("%Y-%m-%d"))
            return
        import numpy as np

        try:
            ds = _open_catalog(dataset)
        except DataError:
            if product != "s2s":
                raise
            # Calendar fallback when the catalog is unreachable. 2-day embargo.
            print((dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)).isoformat())
            return
        latest = np.max(ds["init_time"].values)
        # Read one value: the last lead of the newest init at one grid point.
        # NaN there means the long leads are still publishing.
        var = ds[next(iter(ds.data_vars))].sel(init_time=latest)
        point = var.isel({d: -1 if d == "lead_time" else 0 for d in var.dims})
        if np.isnan(float(point.values)):
            print(
                f"Note: the {np_to_date(latest).isoformat()} init is still publishing; "
                "its long leads are all-NaN for now.",
                file=sys.stderr,
            )
        print(np_to_date(latest).isoformat())
        return

    if len(start_time or []) != len(end_time or []):
        raise UsageError("pass --start-time and --end-time in pairs.")
    windows = list(zip(start_time or [], end_time or [], strict=True))
    if (date is None) == (not windows):
        raise UsageError(
            "pass --date for one init, or --start-time/--end-time for a range of inits (not both)."
        )
    if product in ("s2s", "hres") and bbox is None:
        raise UsageError(f"--bbox is required for --product {product}.")
    max_lead_h = None if max_lead is None else _parse_max_lead(max_lead)
    tokens = _flatten_variable_tokens(variable) or list(DEFAULT_VARIABLES)

    if product == "hres":
        if windows:
            raise UsageError("--product hres takes one --date (and --run), not init windows.")
        run = run or 0
        date_iso = date.isoformat()
        names = _resolve_variables(tokens, HRES_VARIABLES)
        steps = _steps_for(run, max_lead_h)
        if max_lead_h is not None and "precip" in names:
            # precip becomes rates over [step, next step) and loses its last step, so
            # one more step is fetched to keep every step up to --max-lead.
            steps += [s for s in _steps_for(run) if s > max_lead_h][:1]
        groups: dict[tuple, list[str]] = {}
        for name in names:
            spec = HRES_VARIABLES[name]
            groups.setdefault((spec.level_type, spec.levels), []).append(name)

        import xarray as xr
        from ecmwf.opendata import Client

        print(
            f"Fetching ECMWF HRES for bbox={list(bbox)} date={date_iso} run={run:02d}Z "
            f"variables={','.join(names)} steps={steps[0]}..{steps[-1]}h ({len(steps)})",
            file=sys.stderr,
        )
        with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
            parts = []
            for i, ((level_type, levels), group_vars) in enumerate(groups.items()):
                req = _hres_request(date_iso, run, group_vars, level_type, levels, steps)
                grib = Path(tmpdir) / f"hres_{i}.grib2"
                print(f"Retrieving {', '.join(group_vars)} ({level_type})...", file=sys.stderr)
                # Recent inits try ECMWF first (fastest), then AWS; older inits go
                # straight to AWS since ECMWF no longer has them. Each source gets
                # one attempt: under load both answer "503 Slow Down", and waiting
                # it out leaves the user waiting for nothing.
                oldest_on_ecmwf = dt.datetime.now(dt.UTC).date() - dt.timedelta(
                    days=_ECMWF_DAYS_KEPT - 1
                )
                sources = ("ecmwf", "aws") if date >= oldest_on_ecmwf else ("aws",)
                errors = []
                for source in sources:
                    try:
                        client = Client(source=source, maximum_retries=1)
                        client.retrieve(request=req, target=str(grib))
                        break
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{source}: {exc}")
                else:
                    if any("503" in e or "SlowDown" in e for e in errors):
                        hint = (
                            "The server is rate-limiting requests (503 Slow Down): fetch one "
                            "init at a time and try again in a few minutes."
                        )
                    else:
                        hint = (
                            "The init may not be published yet (recent inits lag ~6-9h; try "
                            "--probe-latest) or this is a transport problem."
                        )
                    raise DataError(
                        f"ECMWF Open Data retrieval failed for init {date_iso} {run:02d}Z "
                        f"({'; '.join(errors)}). {hint}"
                    )
                ds = _promote_vertical(_open_grib(grib))
                parts.append(_rename_to_short(ds, group_vars))

            ds = parts[0] if len(parts) == 1 else xr.merge(parts, join="outer", compat="override")
            ds = ds[names]
            ds = bbox_subset(ds, list(bbox), lat_dim="latitude", lon_dim="longitude")
            if "step" not in ds.dims:
                # cfgrib keeps a lone step (--max-lead 0) as a scalar coordinate.
                ds = ds.expand_dims("step")
            # Read one step at a time: cfgrib materializes the full global field for
            # every step/level it is asked for before slicing to the bbox, so one
            # whole-cube read of a pressure-level field needs several GB.
            ds = xr.concat([ds.isel(step=[i]).load() for i in range(ds.sizes["step"])], dim="step")
            ds.attrs.update(Conventions="CF-1.13", weather_skills_source="ecmwf-hres")
            return _standardize(ds).load()

    if product == "s2s":
        starts = [date] if date is not None else [start for start, _ in windows]
        ends = [date] if date is not None else [end for _, end in windows]
        if min(starts) < _CATALOG_START <= max(ends):
            raise UsageError(
                f"split the request at {_CATALOG_START}: inits before it come from the ECMWF "
                "reforecast (10 members, 1..10) and inits from it from the live forecast "
                "(101 members, 0..100), so they do not stack on one number axis. Fetch "
                f"--end-time {_CATALOG_START - dt.timedelta(days=1)} and "
                f"--start-time {_CATALOG_START} in two calls."
            )
        names = [_canonical_name(token) for token in tokens]
        in_hindcast = all(name in _HINDCAST_VARS for name in names)
        if windows and max(ends) < _CATALOG_START and not in_hindcast:
            raise UsageError(
                f"init windows before {_CATALOG_START} read the ECMWF reforecast, which has "
                "precip, t2m and sst only; fetch other fields one --date at a time."
            )
        if max(ends) < _CATALOG_START and in_hindcast:
            ds = _fetch_hindcast(list(dict.fromkeys(names)), bbox, date, windows, max_lead_h)
            if ds is not None:
                return ds
            print(
                f"{date} is not an ECMWF reforecast init for every requested field; using the ECMWF Data Store.",
                file=sys.stderr,
            )

    if product != "s2s" or windows or _catalog_covers(date, tokens):
        source = "ecmwf-s2s" if product == "s2s" else f"dynamical:{dataset}"
        print(
            f"Fetching ECMWF {product} from dynamical:{dataset} "
            f"for bbox={bbox and list(bbox)} variables={','.join(tokens)}",
            file=sys.stderr,
        )
        return _fetch_catalog(dataset, tokens, bbox, date, windows, max_lead_h, source)

    # S2S from the ECMWF Data Store: a pre-2026 --date the reforecast lacks, or fields
    # neither the catalog nor the reforecast has.
    date_iso = date.isoformat()
    area = list(bbox)
    names = _resolve_variables(tokens)
    mixed = sorted(_ACCUMULATED.intersection(names))
    if "precip" in names and mixed:
        raise UsageError(
            f"fetch {', '.join(mixed)} in a separate call from precip: they stay cumulative "
            "since init and would be misaligned with precip's per-step rate."
        )
    latest = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
    if date > latest:
        raise DataError(
            f"init {date_iso} is inside the 2-day S2S real-time embargo; "
            f"the latest init you can request is {latest.isoformat()}."
        )

    require_env("ECMWF_DATASTORES_KEY")
    _ensure_datastores_url()

    import numpy as np
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
            try:
                leg["remote"].download(str(leg["grib"]))
            except Exception as exc:  # noqa: BLE001
                raise DataError(
                    f"ECDS download failed for init {date_iso} ({exc}); "
                    "this is a transport/auth problem."
                ) from None

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
                # Control-only fields keep a length-1 number axis so the merge
                # below leaves members 1..50 NaN instead of dropping `number`.
                ens = cf.expand_dims("number")
            if key[0] == "daily":
                # cfgrib labels a 24 h mean at its window end; relabel to the
                # window start so step 0 is the first 24 h, like precip.
                ens = ens.drop_vars("valid_time", errors="ignore")
                ens = ens.assign_coords(step=ens["step"] - np.timedelta64(24, "h"))
            family_ensembles.append(_rename_to_short(ens, group_vars))
        ds = (
            family_ensembles[0]
            if len(family_ensembles) == 1
            else xr.merge(family_ensembles, join="outer", compat="override")
        )
        ds = ds[names]
        ds.attrs.update(Conventions="CF-1.13", weather_skills_source="ecmwf-s2s")
        if "precip" in ds.data_vars:
            # ECDS GRIB precip is an amount since init.
            ds["precip"].attrs["standard_name"] = "precipitation_amount"
            ds["precip"].attrs["units"] = "kg m-2"
        ds = _standardize(ds)
        if max_lead_h is not None:
            ds = ds.sel(step=slice(None, np.timedelta64(round(max_lead_h * 3600), "s")))
        # Materialize while GRIB files in the temp dir still exist.
        ds = ds.load()

    return ds


if __name__ == "__main__":
    fetch()

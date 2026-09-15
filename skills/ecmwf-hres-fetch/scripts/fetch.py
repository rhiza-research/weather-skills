# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime",
#   # >=0.3.34 for the IFS Cycle 50r1 (2026-05-12) date-aware oper/scda
#   # stream fix in Client.patch_stream — older pins misroute 06/18Z runs.
#   "ecmwf-opendata==0.3.34",
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
"""Fetch a deterministic ECMWF HRES (IFS oper) forecast and write a weather-skills standard dataset Zarr."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset
from weather_skills_core.units import (
    convert_dataarray,
    precip_amounts_to_rates,
    stamp_data_interval,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# HRES steps
_FULL_RUN_STEPS = list(range(0, 144 + 1, 3)) + list(range(150, 360 + 1, 6))
_SHORT_CUTOFF_STEPS = list(range(0, 144 + 1, 3))

PRESSURE_LEVELS = ("1000", "925", "850", "700", "500", "400", "300", "250", "200", "150", "100", "50")

# Keep dim-like coords; drop GRIB filter scalars that collide across parameters.
_KEEP_COORDS = frozenset(
    {
        "time",
        "step",
        "latitude",
        "longitude",
        "valid_time",
        "lat",
        "lon",
        "vertical",
        "isobaricInhPa",
        "isobaricInPa",
        "level",
    }
)


class _Var(NamedTuple):
    param: str  # ECMWF Open Data short param name
    level_type: str = "sfc"  # "sfc" | "pl"
    levels: tuple[str, ...] = ()
    accumulated: bool = False  # cumulative since forecast start (step 0)


# Most-used first, then the rest of the HRES ("oper") single-level and
# pressure-level fields carried by ECMWF Open Data. `-v` / Zarr tokens mirror
# the cfgrib short names used by ecmwf-fetch (t2m, not 2t) where a field also
# exists in that skill, so downstream code can treat them the same way.
VARIABLES: dict[str, _Var] = {
    "tp": _Var("tp", accumulated=True),
    "t2m": _Var("2t"),
    "d2m": _Var("2d"),
    "msl": _Var("msl"),
    "u10": _Var("10u"),
    "v10": _Var("10v"),
    "gust10": _Var("10fg", accumulated=True),
    "sp": _Var("sp"),
    "tcwv": _Var("tcwv"),
    "cape": _Var("cape"),
    "skt": _Var("skt"),
    "ssrd": _Var("ssrd", accumulated=True),
    "strd": _Var("strd", accumulated=True),
    "str": _Var("str", accumulated=True),
    "ttr": _Var("ttr", accumulated=True),
    "t": _Var("t", "pl", PRESSURE_LEVELS),
    "gh": _Var("gh", "pl", PRESSURE_LEVELS),
    "u": _Var("u", "pl", PRESSURE_LEVELS),
    "v": _Var("v", "pl", PRESSURE_LEVELS),
    "q": _Var("q", "pl", PRESSURE_LEVELS),
    "r": _Var("r", "pl", PRESSURE_LEVELS),
}
DEFAULT_VARIABLES = ["tp"]

# cfgrib decodes Open Data GRIB2 with these short names; map back onto the
# canonical `-v` tokens above (mirrors ecmwf-fetch's GRIB alias table).
_GRIB_EXTRAS = {
    "2t": "t2m",
    "2d": "d2m",
    "10u": "u10",
    "10v": "v10",
    "10fg": "gust10",
    "z": "gh",  # cfgrib sometimes decodes HRES gh as geopotential `z` (m2 s-2)
}
_GRIB_ALIASES = {name: name for name in VARIABLES} | _GRIB_EXTRAS

_KELVIN_TEMPS = frozenset({"t2m", "d2m", "skt", "t"})

# Accumulated (non-precip) fields left as raw cumulative-since-init amounts;
# route them through the `deaccumulate` skill downstream if a rate is needed.
_ACCUMULATED_NON_PRECIP = frozenset({"gust10", "ssrd", "strd", "str", "ttr"})


def _canonical_name(token: str) -> str | None:
    if token in VARIABLES:
        return token
    extra = _GRIB_EXTRAS.get(token)
    if extra is not None and extra in VARIABLES:
        return extra
    for short, spec in VARIABLES.items():
        if token == spec.param:
            return short
    return None


def _resolve_variables(raw: list[str] | None) -> list[str]:
    tokens = raw or list(DEFAULT_VARIABLES)
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


def _steps_for(run_hour: int) -> list[int]:
    return _SHORT_CUTOFF_STEPS if run_hour in (6, 18) else _FULL_RUN_STEPS


def _group_for_request(names: list[str]) -> list[tuple[tuple, list[str]]]:
    """Split variables so each Open Data request has one `levtype`/level set."""
    groups: dict[tuple, list[str]] = {}
    for name in names:
        spec = VARIABLES[name]
        groups.setdefault((spec.level_type, spec.levels), []).append(name)
    return list(groups.items())


def _build_request(date_iso: str, run_hour: int, group_vars: list[str], level_type: str, levels: tuple[str, ...]) -> dict:
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
        "step": _steps_for(run_hour),
        "levtype": level_type,
        "param": [VARIABLES[name].param for name in group_vars],
    }
    if levels:
        req["levelist"] = list(levels)
    return req


def _drop_grib_filters(ds):
    drop = [name for name in ds.coords if name not in ds.dims and name not in _KEEP_COORDS]
    return ds.drop_vars(drop) if drop else ds


def _open_grib(path: Path):
    """Decode a (possibly mixed-parameter, multi-step) GRIB2 into one Dataset."""
    import cfgrib
    import xarray as xr

    parts = cfgrib.open_datasets(str(path))
    if not parts:
        raise DataError(f"HRES GRIB {path.name} contained no messages.")
    cleaned = [_drop_grib_filters(part) for part in parts]
    if len(cleaned) == 1:
        return cleaned[0]
    return xr.merge(cleaned, compat="override", combine_attrs="override")


def _promote_vertical(ds):
    """Rename cfgrib pressure coords onto the ontology ``vertical`` dim."""
    if "vertical" in ds.dims:
        return ds
    for name, units in (("isobaricInhPa", "hPa"), ("isobaricInPa", "Pa"), ("level", None)):
        if name not in ds.coords and name not in ds.dims:
            continue
        if name not in ds.dims:
            ds = ds.expand_dims(name)
        ds = ds.rename({name: "vertical"})
        if units:
            ds["vertical"].attrs.setdefault("units", units)
        ds["vertical"].attrs.setdefault("standard_name", "air_pressure")
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
        raise DataError(f"HRES GRIB did not contain {', '.join(missing)} (decoded: {have}).")
    return ds[requested]


def _to_celsius(da):
    units = str(da.attrs.get("units") or "")
    if units in {"degree_Celsius", "degC", "celsius"}:
        return da
    converted, _ = convert_dataarray(da, "degree_Celsius")
    converted.attrs["units"] = "degree_Celsius"
    return converted


def _standardize(ds):
    """CF attrs + standard units."""
    stamp_cf_attrs(ds)
    if "tp" in ds.data_vars:
        ds["tp"].attrs["long_name"] = "Total precipitation"
    ds = to_standard_units(ds)
    for name in _KELVIN_TEMPS:
        if name in ds.data_vars:
            ds[name] = _to_celsius(ds[name])
    ds = precip_amounts_to_rates(ds)
    return stamp_data_interval(ds)


@weather_skill(
    name="ecmwf-hres-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--date", required=True)
@weather_skill.argument(
    "--run",
    type=int,
    default=0,
    choices=(0, 6, 12, 18),
    help=(
        "Init hour (UTC). 00/12 publish the full 15-day range (3-hourly to "
        "144h, 6-hourly to 360h); 06/18 (short cutoff) only to 144h, "
        "3-hourly throughout. Default 0."
    ),
)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help=(
        "HRES field to retrieve (repeatable). Most used first: tp, t2m, msl, "
        "u10, v10. Pressure-level: t, gh, u, v, q, r. Default tp. Names mirror "
        "ecmwf-fetch's cfgrib short names where the field also exists there."
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
        "Print the latest available init as YYYY-MM-DDTHH on stdout and exit. "
        "Does not download fields."
    ),
)
def fetch(bbox, date, run, variable, **kwargs):
    """Fetch a deterministic ECMWF HRES (IFS oper) forecast and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        from ecmwf.opendata import Client

        client = Client(source="ecmwf")
        try:
            latest = client.latest(stream="oper", type="fc", step=0, levtype="sfc", param="2t")
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"could not determine the latest HRES init ({exc}).") from None
        print(latest.strftime("%Y-%m-%dT%H"))
        return

    if bbox is None:
        raise UsageError("--bbox is required (HRES is retrieved as global GRIB2 and subset locally).")

    date_iso = date.isoformat()
    names = _resolve_variables(variable)
    groups = _group_for_request(names)

    import xarray as xr
    from ecmwf.opendata import Client

    print(
        f"Fetching ECMWF HRES for bbox={list(bbox)} date={date_iso} run={run:02d}Z "
        f"variables={','.join(names)}",
        file=sys.stderr,
    )

    client = Client(source="ecmwf")
    with tempfile.TemporaryDirectory(prefix="ecmwf-hres-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        parts = []
        for i, ((level_type, levels), group_vars) in enumerate(groups):
            req = _build_request(date_iso, run, group_vars, level_type, levels)
            grib = tmp / f"hres_{i}.grib2"
            print(f"Retrieving {', '.join(group_vars)} ({level_type})...", file=sys.stderr)
            try:
                client.retrieve(request=req, target=str(grib))
            except Exception as exc:  # noqa: BLE001
                raise DataError(
                    f"ECMWF Open Data retrieval failed for init {date_iso} {run:02d}Z "
                    f"({exc}); the init may not be published yet (recent inits lag ~6-9h) "
                    "or this is a transport problem. Try an older --date or --run."
                ) from None
            ds = _promote_vertical(_open_grib(grib))
            parts.append(_rename_to_short(ds, group_vars))

        ds = parts[0] if len(parts) == 1 else xr.merge(parts, join="outer", compat="override")
        ds = ds[names]
        ds = bbox_subset(ds, list(bbox), lat_dim="latitude", lon_dim="longitude")
        ds.attrs.update(Conventions="CF-1.13", weather_skills_source="ecmwf-hres")
        ds = _standardize(ds)
        ds = ds.load()

    return ds


if __name__ == "__main__":
    fetch()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Compute eastward moisture flux (q·u) and, by default, its column integral (IVT)."""

from __future__ import annotations

import argparse
import sys

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import names_for
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset, ureg

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

# Standard gravity (m s-2). Column integral (1/g) ∫ q u dp then has units kg m-1 s-1.
G = 9.80665

HUMIDITY_NAMES = ("q", "specific_humidity", "hus", "humidity")
WIND_NAMES = ("u", "wind_u", "u_component_of_wind", "ua", "uwnd", "eastward_wind")

_HPA_UNITS = frozenset(
    {"hpa", "millibar", "millibars", "mb", "mbar", "hectopascal", "hectopascals"}
)
_PA_UNITS = frozenset({"pa", "pascal", "pascals", "nm-2", "n/m2"})


def _strip_pint(da: xr.DataArray) -> xr.DataArray:
    if getattr(getattr(da, "pint", None), "units", None) is not None:
        return da.pint.dequantify()
    return da


def _as_datasets(ds) -> list[xr.Dataset]:
    if isinstance(ds, list):
        return ds
    return [ds]


def _vertical_dim(*arrays: xr.DataArray) -> str | None:
    for da in arrays:
        for name in names_for("vertical"):
            if name in da.dims:
                return name
    return None


def _as_pa(vertical: xr.DataArray) -> xr.DataArray:
    """Return the vertical coordinate in Pa (hPa / millibar × 100)."""
    raw = _strip_pint(vertical)
    units = str(raw.attrs.get("units", "")).strip().lower().replace(" ", "")
    pa = raw.astype("float64")
    if units in _PA_UNITS:
        return pa.assign_attrs(units="Pa")
    if units in _HPA_UNITS or units == "":
        if units == "":
            vmax = float(np.nanmax(np.asarray(pa)))
            if np.isfinite(vmax) and vmax > 2000:
                return pa.assign_attrs(units="Pa")
        return (pa * 100.0).assign_attrs(units="Pa")
    raise UsageError(
        f"vertical coordinate units {raw.attrs.get('units')!r} are not pressure "
        "(expected hPa or Pa)."
    )


def _pick_var(
    datasets: list[xr.Dataset],
    explicit: str | None,
    aliases: tuple[str, ...],
    label: str,
    fallback_index: int | None,
) -> xr.DataArray:
    available = sorted({name for item in datasets for name in item.data_vars})
    if explicit:
        for item in datasets:
            if explicit in item.data_vars:
                return item[explicit]
        raise UsageError(f"{label} variable {explicit!r} not found. Available: {available}.")
    for name in aliases:
        for item in datasets:
            if name in item.data_vars:
                return item[name]
    if fallback_index is not None and 0 <= fallback_index < len(datasets):
        item = datasets[fallback_index]
        if len(item.data_vars) == 1:
            return item[next(iter(item.data_vars))]
    flag = "--humidity-variable" if label == "humidity" else "--wind-variable"
    raise UsageError(
        f"could not find a {label} variable (looked for {', '.join(aliases)}). "
        f"Available: {available}. Pass {flag}."
    )


def _column_ivt(qu: xr.DataArray, vdim: str) -> xr.DataArray:
    """(1/g) ∫ q u dp with trapezoidal rule; pressure in Pa."""
    other = [d for d in qu.dims if d != vdim]
    valid = qu.notnull().any(dim=other) if other else qu.notnull()
    empty = qu[vdim].values[~valid.values]
    if empty.size:
        print(
            f"WARNING: dropping {vdim} levels with no finite q*u anywhere: {empty.tolist()}; "
            "the column integral covers the remaining levels only.",
            file=sys.stderr,
        )
    qu = qu.where(valid, drop=True)
    if qu.sizes.get(vdim, 0) < 2:
        raise UsageError(
            "--integrate needs at least two finite pressure levels; "
            "pass --no-integrate for per-level q*u, or fetch a pressure stack "
            "(dynamical-fetch -v q -v u)."
        )
    # Integrate surface-ward over ascending pressure so eastward flux stays
    # positive whichever way the levels are stored (1000→50 or 50→1000 hPa).
    p_pa = _as_pa(qu[vdim])
    column = qu.assign_coords({vdim: p_pa}).sortby(vdim).integrate(vdim)
    return (column / G).assign_attrs(
        units="kg m-1 s-1",
        standard_name="eastward_atmosphere_water_vapor_transport",
        long_name="vertically integrated eastward water vapour flux",
        cell_methods=f"{vdim}: trapezoidal integral (1/g) ∫ q u dp",
    )


@weather_skill(
    name="zonal-moisture-transport",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument(
    "--humidity-variable",
    help="Specific-humidity variable name. Default: q / specific_humidity / hus.",
)
@weather_skill.argument(
    "--wind-variable",
    help="Zonal-wind variable name. Default: u / wind_u / ua / uwnd / eastward_wind.",
)
@weather_skill.argument(
    "--integrate",
    action=argparse.BooleanOptionalAction,
    default=True,
    help=("Column-integrate q*u to eastward IVT (default). Pass --no-integrate for per-level q*u."),
)
@weather_skill.argument(
    "--output-name",
    help="Output variable name. Default viwve (integrated) or qu (per-level).",
)
def zonal_moisture_transport(
    ds,
    humidity_variable,
    wind_variable,
    integrate,
    output_name,
    **kwargs,
):
    """Compute eastward moisture flux (q·u) and, by default, its column integral (IVT)."""
    datasets = []
    for item in _as_datasets(ds):
        item = ensure_normalized_longitude(item)
        # dequantify_dataset keeps each variable's units attr (Dataset.map drops it).
        datasets.append(dequantify_dataset(item))
    if not (1 <= len(datasets) <= 2):
        raise UsageError(f"expected one or two --input paths, got {len(datasets)}")

    fallback_q = 0 if len(datasets) == 2 else None
    fallback_u = 1 if len(datasets) == 2 else None
    q = _pick_var(datasets, humidity_variable, HUMIDITY_NAMES, "humidity", fallback_q)
    u = _pick_var(datasets, wind_variable, WIND_NAMES, "zonal wind", fallback_u)
    # q in g kg-1 or u in km h-1 / knots would scale the flux; convert to SI.
    converted = []
    for da, target in ((q, "kg kg-1"), (u, "m s-1")):
        units = da.attrs.get("units")
        try:
            factor = ureg.Quantity(1.0, units).to(target).magnitude if units else 1.0
        except Exception as exc:  # noqa: BLE001 — unparseable or wrong dimension
            raise UsageError(f"{da.name} units {units!r} do not convert to {target}.") from exc
        converted.append(da.astype("float64") * factor)
    q, u = converted
    q, u = xr.align(q, u, join="inner")
    if q.size == 0 or u.size == 0:
        raise UsageError(
            "humidity and wind share no overlapping coordinates after inner-join alignment."
        )

    qu = (q * u).assign_attrs(
        units="kg kg-1 m s-1",
        long_name="eastward moisture flux",
        standard_name="product_of_eastward_wind_and_specific_humidity",
    )

    vdim = _vertical_dim(qu)
    if integrate:
        if vdim is None or qu.sizes.get(vdim, 0) < 2:
            raise UsageError(
                "--integrate needs a pressure dim with at least two levels; "
                "pass --no-integrate for a single-level q*u field."
            )
        field = _column_ivt(qu, vdim)
        name = output_name or "viwve"
    else:
        field = qu
        name = output_name or "qu"

    return xr.Dataset({name: field})


if __name__ == "__main__":
    zonal_moisture_transport()

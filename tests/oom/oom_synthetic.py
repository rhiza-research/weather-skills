"""Synthetic weather-skills envelope Zarrs of a chosen size, for the OOM suite.

Stores are written one slab at a time with numpy and ``append_dim`` (no dask in
the test env), so building a store bigger than memory never holds more than one
slab. Each store is cached under its kind, size and variant, and reused.

Values are rounded to 0.1 so the stores compress: disk use stays well below
the uncompressed size the skills see in memory.
"""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

import numpy as np
import xarray as xr

# Bump when a builder changes, so cached stores are rebuilt.
FORMAT_VERSION = 2
SLAB_BYTES = 32 * 2**20
# Below this target, build on a coarse grid so graph-tier runs stay tiny. The
# scaling sizes (16 MiB and up) share the fine grid, so fixed per-grid costs
# (coords, masks) cancel out between the small and the large run.
FINE_GRID_MIN_BYTES = 16 * 2**20


def synthetic_zarr(kind: str, target_bytes: int, cache_dir: Path, **variant) -> Path:
    """Path to a cached ``kind`` store holding about ``target_bytes`` of data."""
    if kind not in BUILDERS:
        raise KeyError(f"unknown synthetic kind {kind!r}; have {sorted(BUILDERS)}")
    parts = [kind, str(target_bytes), *(f"{k}={v}" for k, v in sorted(variant.items()))]
    path = Path(cache_dir) / f"{'_'.join(parts)}_v{FORMAT_VERSION}.zarr"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    BUILDERS[kind](tmp, target_bytes, **variant)
    tmp.rename(path)
    return path


def _grid(target_bytes):
    step = 0.5 if target_bytes >= FINE_GRID_MIN_BYTES else 2.0
    lat = np.round(np.arange(-90, 90 + step / 2, step), 3)
    lon = np.round(np.arange(-180, 180, step), 3)
    return lat, lon


def _latlon_coords(lat, lon):
    return {
        "latitude": (
            "latitude",
            lat,
            {"standard_name": "latitude", "units": "degrees_north", "axis": "Y"},
        ),
        "longitude": (
            "longitude",
            lon,
            {"standard_name": "longitude", "units": "degrees_east", "axis": "X"},
        ),
    }


def _values(rng, shape):
    """Gamma-distributed precip-like values, rounded so they compress."""
    return np.round(rng.gamma(0.5, 4.0, size=shape), 1).astype("float32")


def _write_slabs(path, n, slab, make_slab, dim):
    """Write ``make_slab(lo, hi)`` for each slab of ``dim``; the first creates the store."""
    for lo in range(0, n, slab):
        ds = make_slab(lo, min(lo + slab, n))
        if lo == 0:
            encoding = {
                name: {"chunks": tuple(slab if d == dim else ds.sizes[d] for d in var.dims)}
                for name, var in ds.data_vars.items()
            }
            ds.to_zarr(path, mode="w", consolidated=True, encoding=encoding)
        else:
            ds.to_zarr(path, append_dim=dim, consolidated=True)


def build_grid(
    path,
    target_bytes,
    *,
    name="precip",
    units="mm day-1",
    standard_name="lwe_precipitation_rate",
    start="2000-01-01",
    seed=0,
    offset=0.0,
    period_days=None,
    labels=False,
):
    """Observations: ``(time, latitude, longitude)``, daily from ``start``.

    ``offset`` is added to every value (e.g. 273.1 for a temperature in K).
    ``period_days`` stamps ``aggregation_period``/``cell_methods`` as if
    aggregate-temporal had made the series (for convert-to-totals).
    ``labels`` adds label-group style coords: ``month`` along time and a
    ``region`` (west/east hemisphere) on the grid, for summarize-dim ``--by``.
    """
    lat, lon = _grid(target_bytes)
    field = lat.size * lon.size * 4
    n_time = _n_time(target_bytes, field)
    slab = max(SLAB_BYTES // field, 1)
    t0 = np.datetime64(start, "ns")
    attrs = {"units": units, "standard_name": standard_name}
    if period_days is not None:
        attrs |= {"aggregation_period": f"{period_days} day", "cell_methods": "time: mean"}
    region = np.where(lon[None, :] < 0, "west", "east").repeat(lat.size, axis=0)

    def make_slab(lo, hi):
        rng = np.random.default_rng([seed, lo])
        times = t0 + np.arange(lo, hi) * np.timedelta64(1, "D")
        values = _values(rng, (hi - lo, lat.size, lon.size))
        if offset:
            values += np.float32(offset)
        coords = {
            "time": ("time", times, {"standard_name": "time", "axis": "T"}),
            **_latlon_coords(lat, lon),
        }
        if labels:
            coords["month"] = ("time", times.astype("datetime64[M]").astype(int) % 12 + 1)
            coords["region"] = (("latitude", "longitude"), region)
        return xr.Dataset({name: (("time", "latitude", "longitude"), values, attrs)}, coords=coords)

    _write_slabs(path, n_time, slab, make_slab, "time")


def _n_time(target_bytes, field):
    """Daily steps so one ``field``-byte map per day reaches ``target_bytes``."""
    return max(math.ceil(target_bytes / field), 8)


def build_t2m_grid(path, target_bytes, **variant):
    """2 m temperature in K on the ``grid`` layout (values ~273-300 K)."""
    variant = {
        "name": "t2m",
        "units": "K",
        "standard_name": "air_temperature",
        "offset": 273.1,
        **variant,
    }
    build_grid(path, target_bytes, **variant)


def build_climatology(path, target_bytes, *, name="precip", start="2000-01-01", seed=0):
    """clim-fetch style ``<name>_avg`` / ``<name>_std`` on the same days and grid as ``grid``.

    The time axis matches ``grid`` at the same ``target_bytes`` label for label,
    so standardize-anomaly can pair them; the store holds two such variables.
    """
    lat, lon = _grid(target_bytes)
    field = lat.size * lon.size * 4
    n_time = _n_time(target_bytes, field)
    slab = max(SLAB_BYTES // (2 * field), 1)
    t0 = np.datetime64(start, "ns")
    attrs = {"units": "mm day-1", "aggregation_period": "1 day", "data_interval": "1 day"}

    def make_slab(lo, hi):
        rng = np.random.default_rng([seed, lo])
        shape = (hi - lo, lat.size, lon.size)
        times = t0 + np.arange(lo, hi) * np.timedelta64(1, "D")
        dims = ("time", "latitude", "longitude")
        return xr.Dataset(
            {
                f"{name}_avg": (dims, _values(rng, shape), attrs),
                # Strictly positive spread, so every cell has a z-score.
                f"{name}_std": (dims, _values(rng, shape) + np.float32(0.5), attrs),
            },
            coords={
                "time": ("time", times, {"standard_name": "time", "axis": "T"}),
                **_latlon_coords(lat, lon),
            },
        )

    _write_slabs(path, n_time, slab, make_slab, "time")


def build_pressure_levels(
    path, target_bytes, *, levels=(1000, 925, 850, 700, 600, 500, 400, 300), seed=0
):
    """Specific humidity ``q`` and zonal wind ``u``: ``(time, level, latitude, longitude)``.

    ``target_bytes`` covers both variables together.
    """
    lat, lon = _grid(target_bytes)
    level = np.asarray(levels, dtype="float64")
    step = 2 * level.size * lat.size * lon.size * 4
    n_time = max(math.ceil(target_bytes / step), 4)
    slab = max(SLAB_BYTES // step, 1)
    t0 = np.datetime64("2000-01-01", "ns")
    dims = ("time", "level", "latitude", "longitude")

    def make_slab(lo, hi):
        rng = np.random.default_rng([seed, lo])
        shape = (hi - lo, level.size, lat.size, lon.size)
        times = t0 + np.arange(lo, hi) * np.timedelta64(6, "h")
        q = _values(rng, shape) * np.float32(1e-3)
        u = np.round(rng.normal(5.0, 8.0, size=shape), 0).astype("float32")
        return xr.Dataset(
            {
                "q": (dims, q, {"units": "kg kg-1", "standard_name": "specific_humidity"}),
                "u": (dims, u, {"units": "m s-1", "standard_name": "eastward_wind"}),
            },
            coords={
                "time": ("time", times, {"standard_name": "time", "axis": "T"}),
                "level": (
                    "level",
                    level,
                    {
                        "units": "hPa",
                        "standard_name": "air_pressure",
                        "positive": "down",
                        "axis": "Z",
                    },
                ),
                **_latlon_coords(lat, lon),
            },
        )

    _write_slabs(path, n_time, slab, make_slab, "time")


def build_ensemble_forecast(
    path,
    target_bytes,
    *,
    name="tp",
    n_step=46,
    init="2026-01-01",
    seed=0,
    accumulated=True,
):
    """ECMWF-style forecast: ``(number, step, latitude, longitude)``, scalar ``time`` init.

    ``tp`` is accumulated since init (mm), non-decreasing along ``step``; with
    ``accumulated=False`` it is instead a daily rate (mm day-1) per step.
    The member count grows to reach ``target_bytes``.
    """
    lat, lon = _grid(target_bytes)
    member = n_step * lat.size * lon.size * 4
    n_member = max(math.ceil(target_bytes / member), 1)
    steps = np.arange(n_step) * np.timedelta64(1, "D")
    if accumulated:
        attrs = {"units": "mm", "long_name": "Total precipitation"}
    else:
        attrs = {
            "units": "mm day-1",
            "standard_name": "lwe_precipitation_rate",
            "long_name": "Precipitation rate",
            "data_interval": "1 day",
        }

    def make_slab(lo, hi):
        rng = np.random.default_rng([seed, lo])
        data = _values(rng, (hi - lo, n_step, lat.size, lon.size))
        if accumulated:
            data = np.cumsum(data, axis=1)
        return xr.Dataset(
            {name: (("number", "step", "latitude", "longitude"), data, attrs)},
            coords={
                "number": ("number", np.arange(lo, hi)),
                "step": (
                    "step",
                    steps.astype("timedelta64[ns]"),
                    {
                        "standard_name": "forecast_period",
                        "long_name": "time since forecast_reference_time",
                    },
                ),
                "time": (
                    (),
                    np.datetime64(init, "ns"),
                    {
                        "standard_name": "forecast_reference_time",
                        "axis": "T",
                    },
                ),
                **_latlon_coords(lat, lon),
            },
        )

    _write_slabs(path, n_member, 1, make_slab, "number")


def build_point_obs(
    path,
    target_bytes,
    *,
    name="precip",
    n_points=None,
    start="1900-01-01",
    seed=0,
):
    """Stations: ``(point_id, time)`` with per-station lat/lon, daily from ``start``."""
    if n_points is None:
        n_points = 20_000 if target_bytes >= FINE_GRID_MIN_BYTES else 200
    rng = np.random.default_rng(seed)
    lats = np.round(rng.uniform(-60, 70, n_points), 4)
    lons = np.round(rng.uniform(-180, 180, n_points), 4)
    ids = np.array([f"S{i:06d}" for i in range(n_points)])
    row = n_points * 4
    n_time = max(math.ceil(target_bytes / row), 8)
    slab = max(SLAB_BYTES // row, 1)
    t0 = np.datetime64(start, "ns")
    attrs = {"units": "mm day-1", "standard_name": "lwe_precipitation_rate"}

    def make_slab(lo, hi):
        slab_rng = np.random.default_rng([seed, lo])
        times = t0 + np.arange(lo, hi) * np.timedelta64(1, "D")
        return xr.Dataset(
            {name: (("point_id", "time"), _values(slab_rng, (n_points, hi - lo)), attrs)},
            coords={
                "point_id": ids,
                "time": ("time", times, {"standard_name": "time", "axis": "T"}),
                "latitude": (
                    "point_id",
                    lats,
                    {"standard_name": "latitude", "units": "degrees_north"},
                ),
                "longitude": (
                    "point_id",
                    lons,
                    {"standard_name": "longitude", "units": "degrees_east"},
                ),
            },
        )

    _write_slabs(path, n_time, slab, make_slab, "time")


BUILDERS = {
    "grid": build_grid,
    "t2m_grid": build_t2m_grid,
    "climatology": build_climatology,
    "pressure_levels": build_pressure_levels,
    "ensemble_forecast": build_ensemble_forecast,
    "point_obs": build_point_obs,
}

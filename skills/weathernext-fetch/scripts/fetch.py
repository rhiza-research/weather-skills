# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "xarray",
#   "zarr>=3",
#   "cftime",
#   "dask",
#   "gcsfs",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a Google WeatherNext 2 or 3 forecast and write a weather-skills standard dataset."""

from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import (
    precip_amounts_to_rates,
    stamp_data_interval,
    stamp_precip_amounts,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_STORE_NAME = "predictions.zarr"
_STAMP_RE = re.compile(r"^(\d{8})_(\d{2})hr_01_preds$")
_SYNOPTIC_CYCLES = ("00", "06", "12", "18")
_ALL_CYCLES = tuple(f"{h:02d}" for h in range(24))
_STATISTICS = ("mean", "p10", "p25", "p50", "p75", "p90")
# Latest-init search lists one UTC day per request (WeatherNext 3 adds an init
# folder every hour, so a full listing is thousands of entries and ~1 min).
_LATEST_LOOKBACK_DAYS = 14


class _Product(NamedTuple):
    """One WeatherNext realtime archive: where it lives and how it is shaped."""

    key: str
    bucket: str
    prefix: str
    metadata_file: str  # consolidated-metadata object that marks a complete store
    requester_pays: bool
    cycles: tuple[str, ...]
    precip_interval: str
    ensemble: bool
    title: str
    source_tag: str


_PRODUCTS = {
    "2/ensemble": _Product(
        key="2/ensemble",
        bucket="weathernext",
        prefix="weathernext_2_0_0/zarr/2025_to_present",
        metadata_file=".zmetadata",
        requester_pays=False,
        cycles=_SYNOPTIC_CYCLES,
        precip_interval="6 hour",
        ensemble=True,
        title="Google WeatherNext 2 ensemble forecast",
        source_tag="weathernext",
    ),
    "3/ensemble": _Product(
        key="3/ensemble",
        bucket="weathernext3_spatial",
        prefix="weathernext_3_0_0/zarr/2026_to_present",
        metadata_file="zarr.json",
        requester_pays=True,
        cycles=_ALL_CYCLES,
        precip_interval="1 hour",
        ensemble=True,
        title="Google WeatherNext 3 ensemble forecast",
        source_tag="weathernext3",
    ),
    "3/statistics": _Product(
        key="3/statistics",
        bucket="weathernext3_statistics_spatial",
        prefix="weathernext_3_0_0_statistics/zarr/2026_to_present",
        metadata_file="zarr.json",
        requester_pays=False,
        cycles=_ALL_CYCLES,
        precip_interval="1 hour",
        ensemble=False,
        title="Google WeatherNext 3 ensemble statistics forecast",
        source_tag="weathernext3-statistics",
    ),
}

_V2_NATIVE_PRECIP = "total_precipitation_6hr"

# WeatherNext 3 native name -> output name. Fields WeatherNext 2 / ARCO-ERA5
# also carry take their ERA5-lineage names, so v2 and v3 outputs line up.
_V3_RENAME = {
    "temperature_2m": "2m_temperature",
    "dewpoint_temperature_2m": "2m_dewpoint_temperature",
    "u_component_of_wind_10m": "10m_u_component_of_wind",
    "v_component_of_wind_10m": "10m_v_component_of_wind",
    "u_component_of_wind_100m": "100m_u_component_of_wind",
    "v_component_of_wind_100m": "100m_v_component_of_wind",
    "wind_speed_10m": "10m_wind_speed",
    "wind_speed_100m": "100m_wind_speed",
    "total_precipitation_1hr": "tp",
    "experimental_tp_1hr": "experimental_tp",
    "imerg_tp_1hr": "imerg_tp",
}

# Output variable -> (units, standard_name, long_name). WeatherNext 2 stores
# carry no attrs at all, so every field is stamped here (values match
# ARCO-ERA5's own metadata for the same ERA5-lineage fields, for cross-source
# consistency). WeatherNext 3 stores do carry units, but in a non-CF spelling
# (``m s**-1``, ``(0 - 1)``), so the same table overrides them.
_NATIVE_ATTRS = {
    "2m_temperature": ("K", "air_temperature", "2 metre temperature"),
    "2m_dewpoint_temperature": ("K", "dew_point_temperature", "2 metre dewpoint temperature"),
    "station_head_temperature_2m": ("K", "air_temperature", "Station head 2 metre temperature"),
    "station_head_dewpoint_temperature_2m": (
        "K",
        "dew_point_temperature",
        "Station head 2 metre dewpoint temperature",
    ),
    "10m_u_component_of_wind": ("m s-1", "eastward_wind", "10 metre U wind component"),
    "10m_v_component_of_wind": ("m s-1", "northward_wind", "10 metre V wind component"),
    "100m_u_component_of_wind": ("m s-1", "eastward_wind", "100 metre U wind component"),
    "100m_v_component_of_wind": ("m s-1", "northward_wind", "100 metre V wind component"),
    "10m_wind_speed": ("m s-1", "wind_speed", "10 metre wind speed"),
    "100m_wind_speed": ("m s-1", "wind_speed", "100 metre wind speed"),
    "mean_sea_level_pressure": ("Pa", "air_pressure_at_mean_sea_level", "Mean sea level pressure"),
    "sea_surface_temperature": ("K", "sea_surface_temperature", "Sea surface temperature"),
    "total_cloud_cover": ("1", "cloud_area_fraction", "Total cloud cover"),
    "low_cloud_cover": ("1", "low_type_cloud_area_fraction", "Low cloud cover"),
    "medium_cloud_cover": ("1", "medium_type_cloud_area_fraction", "Medium cloud cover"),
    "high_cloud_cover": ("1", "high_type_cloud_area_fraction", "High cloud cover"),
    "surface_solar_radiation_downwards_1hr": (
        "J m-2",
        "integral_wrt_time_of_surface_downwelling_shortwave_flux_in_air",
        "Surface solar radiation downwards (1hr accumulation)",
    ),
    "total_sky_direct_solar_radiation_at_surface_1hr": (
        "J m-2",
        "integral_wrt_time_of_surface_direct_downwelling_shortwave_flux_in_air",
        "Total sky direct solar radiation at surface (1hr accumulation)",
    ),
    "specific_humidity": ("kg kg-1", "specific_humidity", "Specific humidity"),
    "temperature": ("K", "air_temperature", "Temperature"),
    "temperature_300hPa": ("K", "air_temperature", "Temperature at 300 hPa"),
    "temperature_500hPa": ("K", "air_temperature", "Temperature at 500 hPa"),
    "u_component_of_wind": ("m s-1", "eastward_wind", "U component of wind"),
    "v_component_of_wind": ("m s-1", "northward_wind", "V component of wind"),
    "u_component_of_wind_1000hPa": ("m s-1", "eastward_wind", "U component of wind at 1000 hPa"),
    "v_component_of_wind_1000hPa": ("m s-1", "northward_wind", "V component of wind at 1000 hPa"),
    "vertical_velocity": ("Pa s-1", "lagrangian_tendency_of_air_pressure", "Vertical velocity"),
    "geopotential": ("m2 s-2", "geopotential", "Geopotential"),
    "geopotential_300hPa": ("m2 s-2", "geopotential", "Geopotential at 300 hPa"),
    "geopotential_500hPa": ("m2 s-2", "geopotential", "Geopotential at 500 hPa"),
}

_PRECIP_NAMES = ("tp", "experimental_tp", "imerg_tp")
_PRECIP_LONG_NAMES = {
    "tp": "Total precipitation",
    "experimental_tp": "Experimental total precipitation",
    "imerg_tp": "IMERG-calibrated total precipitation",
}

# Aliases shared by both versions, resolved to the *output* name.
_VAR_ALIASES = {
    "t2m": "2m_temperature",
    "temperature_2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "dewpoint_temperature_2m": "2m_dewpoint_temperature",
    "u10": "10m_u_component_of_wind",
    "u_component_of_wind_10m": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "v_component_of_wind_10m": "10m_v_component_of_wind",
    "u100": "100m_u_component_of_wind",
    "u_component_of_wind_100m": "100m_u_component_of_wind",
    "v100": "100m_v_component_of_wind",
    "v_component_of_wind_100m": "100m_v_component_of_wind",
    "si10": "10m_wind_speed",
    "wind_speed_10m": "10m_wind_speed",
    "si100": "100m_wind_speed",
    "wind_speed_100m": "100m_wind_speed",
    "msl": "mean_sea_level_pressure",
    "sst": "sea_surface_temperature",
    "tcc": "total_cloud_cover",
    "lcc": "low_cloud_cover",
    "mcc": "medium_cloud_cover",
    "hcc": "high_cloud_cover",
    "ssrd": "surface_solar_radiation_downwards_1hr",
    "tp": "tp",
    "precip": "tp",
    "precipitation": "tp",
    "total_precipitation": "tp",
    _V2_NATIVE_PRECIP: "tp",
    "total_precipitation_1hr": "tp",
    "pr": "tp",
    "experimental_tp_1hr": "experimental_tp",
    "imerg_tp_1hr": "imerg_tp",
    "q": "specific_humidity",
    "t": "temperature",
    "u": "u_component_of_wind",
    "v": "v_component_of_wind",
    "w": "vertical_velocity",
    "z": "geopotential",
    "z300": "geopotential_300hPa",
    "z500": "geopotential_500hPa",
    "t300": "temperature_300hPa",
    "t500": "temperature_500hPa",
    "u1000": "u_component_of_wind_1000hPa",
    "v1000": "v_component_of_wind_1000hPa",
}

_GRID_RE = re.compile(r"^lat_(0p\d+)$")
_GRID_LABELS = {"0p05": "0.05deg", "0p1": "0.1deg", "0p25": "0.25deg"}
# Default WeatherNext 3 field group when no --variable is given: the hourly
# 0.1deg surface fields (every field but station-head 0.05deg and 0.25deg upper air).
_V3_DEFAULT_GRID = "0p1"


def _auth_msg(product: _Product) -> str:
    msg = (
        f"cannot read gs://{product.bucket} (requires an authenticated Google Cloud "
        "identity, not anonymous access). Set GOOGLE_APPLICATION_CREDENTIALS to a "
        "service-account JSON, or run `gcloud auth application-default login`."
    )
    if product.requester_pays:
        msg += (
            " This bucket is requester pays: pass --billing-project (or set "
            "GOOGLE_CLOUD_PROJECT) to a project with billing enabled."
        )
    return msg


def _resolve_product(version: str, product_name: str | None) -> _Product:
    name = product_name or "ensemble"
    if version == "2" and name != "ensemble":
        raise UsageError("--product statistics is WeatherNext 3 only (use --version 3).")
    return _PRODUCTS[f"{version}/{name}"]


def _billing_project(explicit: str | None) -> str | None:
    return (
        explicit
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("CLOUDSDK_CORE_PROJECT")
    )


def _filesystem(product: _Product, billing_project: str | None = None):
    import gcsfs

    if product.requester_pays and not billing_project:
        raise UsageError(
            f"gs://{product.bucket} is requester pays: pass --billing-project PROJECT_ID "
            "(or set GOOGLE_CLOUD_PROJECT). Reads are billed to that project."
        )
    kwargs = {"requester_pays": billing_project} if product.requester_pays else {}
    try:
        return gcsfs.GCSFileSystem(**kwargs)
    except Exception as exc:  # noqa: BLE001 — surface ADC failures as DataError
        raise DataError(f"{_auth_msg(product)} ({type(exc).__name__}: {exc})") from None


def _gcs_error(exc: Exception, what: str, product: _Product) -> DataError:
    text = f"{type(exc).__name__}: {exc}"
    hint = ""
    lowered = text.lower()
    tokens = (
        "401",
        "403",
        "forbidden",
        "credential",
        "anonymous",
        "requester pays",
        "user project",
    )
    if any(token in lowered for token in tokens):
        hint = f" {_auth_msg(product)}"
    return DataError(f"{what} ({text}).{hint}")


def _store_url(stamp: str, product: _Product) -> str:
    return f"gs://{product.bucket}/{product.prefix}/{stamp}/{_STORE_NAME}"


def _list_init_stamps(
    product: _Product, billing_project: str | None = None, day: str | None = None
) -> list[str]:
    """Init stamps in the archive, or only those starting with ``day`` (YYYYMMDD)."""
    fs = _filesystem(product, billing_project)
    prefix = f"{product.bucket}/{product.prefix}"
    try:
        items = fs.ls(prefix, detail=False, prefix=day or "", refresh=True)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"GCS listing failed for gs://{prefix}", product) from None
    stamps = []
    for item in items:
        name = str(item).rstrip("/").rsplit("/", 1)[-1]
        if _STAMP_RE.fullmatch(name):
            stamps.append(name)
    stamps.sort()
    return stamps


def _store_exists(stamp: str, product: _Product, billing_project: str | None = None) -> bool:
    fs = _filesystem(product, billing_project)
    key = f"{product.bucket}/{product.prefix}/{stamp}/{_STORE_NAME}/{product.metadata_file}"
    try:
        return bool(fs.exists(key))
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"GCS HEAD failed for gs://{key}", product) from None


def _stamp_to_date(stamp: str) -> str:
    match = _STAMP_RE.fullmatch(stamp)
    if not match:
        raise DataError(f"unrecognized WeatherNext init stamp {stamp!r}")
    compact = match.group(1)
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"


def _latest_stamp(product: _Product, cycle: str | None, billing_project: str | None) -> str | None:
    """Newest complete store for ``cycle`` (default: a 00/06/12/18 full-length init).

    Walks back one UTC day at a time, then falls back to a full listing.
    """
    import datetime as dt

    wanted = (cycle,) if cycle else _SYNOPTIC_CYCLES
    today = dt.datetime.now(dt.UTC).date()
    days = [(today - dt.timedelta(days=n)).strftime("%Y%m%d") for n in range(_LATEST_LOOKBACK_DAYS)]
    for day in [*days, None]:
        stamps = _list_init_stamps(product, billing_project, day)
        for stamp in reversed(stamps):
            if _STAMP_RE.fullmatch(stamp).group(2) in wanted and _store_exists(
                stamp, product, billing_project
            ):
                return stamp
    return None


def _check_cycle(product: _Product, cycle: str | None) -> None:
    if cycle and cycle not in product.cycles:
        raise UsageError(
            f"--cycle {cycle} is not published for WeatherNext {product.key.split('/')[0]} "
            f"(inits: {', '.join(product.cycles)} UTC)."
        )


def _resolve_stamp(date, cycle: str | None, product: _Product, billing_project: str | None) -> str:
    if date is not None:
        hour = cycle or "00"
        stamp = f"{date.strftime('%Y%m%d')}_{hour}hr_01_preds"
        if not _store_exists(stamp, product, billing_project):
            raise DataError(
                f"no store at {_store_url(stamp, product)}. Pick a published init "
                "(`--probe-latest`), or omit --date to take the latest. Inits are "
                f"{'/'.join(_SYNOPTIC_CYCLES)} UTC (`--cycle`)"
                + (
                    "; WeatherNext 3 also has 48h interim inits every other hour"
                    if product.cycles == _ALL_CYCLES
                    else ""
                )
                + f". Only the {product.prefix.rsplit('/', 1)[-1]} realtime archive is supported by this skill."
            )
        return stamp

    stamp = _latest_stamp(product, cycle, billing_project)
    if stamp is not None:
        return stamp
    raise DataError(f"no predictions store under gs://{product.bucket}/{product.prefix}/.")


def _open_remote(stamp: str, product: _Product, billing_project: str | None = None):
    import xarray as xr

    url = _store_url(stamp, product)
    try:
        fs = _filesystem(product, billing_project)
        # Dask-backed with the store's native chunks, so clip/stack stay lazy and
        # only chunks touched by the --member/--variable/--bbox subset are read.
        return xr.open_zarr(fs.get_mapper(url), consolidated=True, chunks={})
    except (DataError, UsageError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"failed to open remote store {url}", product) from None


def _as_hours(values):
    """Lead coord values (decoded timedelta or raw integer hours) → timedelta64[ns]."""
    import numpy as np

    values = np.asarray(values)
    if values.dtype.kind == "m":
        return values.astype("timedelta64[ns]")
    return (values.astype("int64") * np.timedelta64(1, "h")).astype("timedelta64[ns]")


def _shift_step_origin_to_zero(ds):
    """Relabel ``step`` so the first tick is lead 0 (period start)."""
    import numpy as np

    if "step" not in ds.dims or ds.sizes["step"] == 0:
        return ds
    steps = np.asarray(ds["step"].values)
    first = steps[0]
    zero = np.asarray(0).astype(steps.dtype)
    if first == zero:
        return ds
    new_step = steps - first
    attrs = dict(ds["step"].attrs)
    out = ds.assign_coords(step=("step", new_step))
    out["step"].attrs.update(attrs)
    return out


def _split_statistic(name: str) -> tuple[str, str | None]:
    for stat in _STATISTICS:
        if name.endswith(f"_{stat}"):
            return name[: -len(stat) - 1], stat
    return name, None


def _v3_output_name(native: str) -> str:
    base, stat = _split_statistic(native)
    out = _V3_RENAME.get(base, base)
    return f"{out}_{stat}" if stat else out


def _field_group(da) -> tuple[str | None, bool]:
    """(grid id, hourly) for a WeatherNext 3 variable.

    Ensemble surface fields are hourly via ``lead_subtime``; ensemble
    pressure-level fields (on ``level``) are 6-hourly; the statistics store is
    hourly throughout.
    """
    grid = next((m.group(1) for d in da.dims if (m := _GRID_RE.match(d))), None)
    return grid, "lead_subtime" in da.dims or "level" not in da.dims


def _group_label(group) -> str:
    grid, hourly = group
    return f"{_GRID_LABELS.get(grid, grid)} {'hourly' if hourly else '6-hourly'}"


def _select_v2(ds, variable):
    if _V2_NATIVE_PRECIP in ds.data_vars:
        ds = ds.rename({_V2_NATIVE_PRECIP: "tp"})
    if not variable:
        return ds
    resolved, missing = [], []
    for raw in variable:
        name = _VAR_ALIASES.get(raw, raw)
        if name not in ds.data_vars:
            missing.append(raw)
        elif name not in resolved:
            resolved.append(name)
    if missing:
        raise UsageError(
            f"variable(s) not in this WeatherNext product: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(ds.data_vars))}"
        )
    return ds[resolved]


def _select_v3(ds, variable, statistic):
    """Pick WeatherNext 3 fields by output name, renamed, all on one grid and lead axis."""
    by_base: dict[str, list[str]] = {}
    for native in ds.data_vars:
        out = _v3_output_name(native)
        by_base.setdefault(_split_statistic(out)[0], []).append(native)

    if variable:
        bases, missing = [], []
        for raw in variable:
            name = _VAR_ALIASES.get(raw, raw)
            name = _split_statistic(_v3_output_name(name))[0]
            if name not in by_base:
                missing.append(raw)
            elif name not in bases:
                bases.append(name)
        if missing:
            raise UsageError(
                f"variable(s) not in this WeatherNext product: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(by_base))}"
            )
    else:
        bases = [
            b for b, names in by_base.items() if _field_group(ds[names[0]])[0] == _V3_DEFAULT_GRID
        ]

    natives = [n for b in bases for n in by_base[b]]
    if statistic:
        natives = [n for n in natives if _split_statistic(n)[1] in statistic]

    groups: dict[tuple, list[str]] = {}
    for native in natives:
        groups.setdefault(_field_group(ds[native]), []).append(_v3_output_name(native))
    if len(groups) > 1:
        detail = "; ".join(
            f"{_group_label(g)}: {', '.join(sorted(set(_split_statistic(n)[0] for n in names)))}"
            for g, names in groups.items()
        )
        raise UsageError(
            "WeatherNext 3 stores these variables on different grids or lead axes, so they "
            f"cannot share one dataset — fetch each group separately ({detail})."
        )

    grid = next(iter(groups))[0] if groups else None
    ds = ds[natives].rename({n: _v3_output_name(n) for n in natives})
    if grid:
        ds = ds.rename({f"lat_{grid}": "lat", f"lon_{grid}": "lon"})
    return ds


def _flatten_leads(ds):
    """WeatherNext 3 ``lead_time`` (x ``lead_subtime``) → one ``step`` axis of lead hours."""
    if "lead_subtime" in ds.dims:
        ds = ds.stack(step=("lead_time", "lead_subtime"), create_index=False)
        hours = _as_hours(ds["lead_time"].values) + _as_hours(ds["lead_subtime"].values)
        ds = ds.drop_vars(["lead_time", "lead_subtime"]).assign_coords(step=("step", hours))
    else:
        hours = _as_hours(ds["lead_time"].values)
        ds = ds.rename({"lead_time": "step"}).assign_coords(step=("step", hours))
    return ds


def _prepare_dataset(ds, product: _Product, variable=None, statistic=None):
    """Native WeatherNext cube → standard ensemble-forecast dims and attrs."""
    import numpy as np

    if product.key.startswith("3/"):
        ds = _select_v3(ds, variable, statistic)
        ds = ds.drop_vars([c for c in ("datetime",) if c in ds.variables])
        ds = _flatten_leads(ds)
        rename = {"sample": "number", "init_time": "time"}
    else:
        ds = _select_v2(ds, variable)
        rename = {"sample": "number", "time": "step", "datetime": "valid_time", "init_time": "time"}
    rename = {src: dst for src, dst in rename.items() if src in ds.dims or src in ds.variables}
    ds = ds.rename(rename)

    if "step" in ds.coords:
        raw = np.asarray(ds["step"].values).astype("timedelta64[ns]")
        ds = ds.assign_coords(step=("step", raw))
        ds["step"].attrs = {}
        ds["step"].attrs.update(
            standard_name="forecast_period",
            long_name="time since forecast_reference_time",
        )
    if "valid_time" not in ds.coords and "time" in ds.coords and "step" in ds.coords:
        ds = ds.assign_coords(valid_time=("step", ds["time"].values + ds["step"].values))
    if "time" in ds.coords:
        ds["time"].attrs = {
            k: v for k, v in ds["time"].attrs.items() if k not in ("units", "calendar")
        }
        ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    if "valid_time" in ds.coords:
        ds["valid_time"].attrs = {}
        ds["valid_time"].attrs.update(standard_name="time", long_name="valid time of forecast step")
    if "level" in ds.coords:
        ds["level"].attrs.update(
            standard_name="air_pressure",
            units="hPa",
            positive="down",
            axis="Z",
            long_name="pressure level",
        )
    for coord in ("lat", "lon"):
        if coord in ds.coords:
            ds[coord].attrs = {}

    for name in ds.data_vars:
        base, stat = _split_statistic(name)
        if base in _PRECIP_NAMES:
            ds[name] = ds[name].clip(min=0)
            ds[name].attrs.update(
                units="m",
                standard_name="lwe_thickness_of_precipitation_amount",
                long_name=_PRECIP_LONG_NAMES[base],
            )
        elif base in _NATIVE_ATTRS:
            units, standard_name, long_name = _NATIVE_ATTRS[base]
            ds[name].attrs.update(units=units, standard_name=standard_name, long_name=long_name)
        else:
            continue
        if stat:
            ds[name].attrs["long_name"] += (
                " (ensemble mean)" if stat == "mean" else f" (ensemble {stat[1:]}th percentile)"
            )

    stamp_cf_attrs(ds)

    order = [d for d in ("number", "step", "level", "lat", "lon") if d in ds.dims]
    if order:
        ds = ds.transpose(*order)
    return ds


def _size_note(product: _Product) -> str:
    if product.key == "2/ensemble":
        return (
            "WeatherNext 2 is 64 members x 60 leads x 0.25deg global, chunked per "
            "member/lead (a --bbox does not shrink the download) — pass -v and --member "
            "to keep the read small."
        )
    if product.key == "3/ensemble":
        return (
            "WeatherNext 3 is 64 members x 360 hourly leads x 0.1deg global, chunked per "
            "member per 6h lead block (a --bbox does not shrink the download, and the bucket "
            "is requester pays) — pass -v and --member to keep the read small. Defaulted to "
            "the 0.1deg hourly surface fields."
        )
    return (
        "WeatherNext 3 statistics are 360 hourly leads x 0.1deg global, chunked per lead "
        "(a --bbox does not shrink the download) — pass -v and --statistic to keep the "
        "read small. Defaulted to the 0.1deg surface fields."
    )


@weather_skill(
    name="weathernext-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--version",
    dest="model_version",
    choices=["2", "3"],
    default="2",
    help="WeatherNext model generation. 2 (default): gs://weathernext/weathernext_2_0_0. "
    "3: gs://weathernext3_spatial (ensemble) or gs://weathernext3_statistics_spatial "
    "(--product statistics).",
)
@weather_skill.argument(
    "--product",
    choices=["ensemble", "statistics"],
    default=None,
    help="WeatherNext 3 only. ensemble (default): 64 members, requester pays. "
    "statistics: precomputed ensemble mean and p10/p25/p50/p75/p90, no members, free to read.",
)
@weather_skill.argument("--date")
@weather_skill.argument(
    "--cycle",
    choices=list(_ALL_CYCLES),
    default=None,
    help="Init hour UTC. Version 2: 00/06/12/18. Version 3 adds 48h interim inits at "
    "every other hour. Default: 00 with --date; the latest 00/06/12/18 init when "
    "--date is omitted.",
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--member",
    type=int,
    action="append",
    help="Restrict to these 0-based ensemble member indices (repeatable). "
    "Default: all 64 members. Selected before download — unlike --bbox, this is "
    "what actually cuts read size (the store's chunks span the full global grid, "
    "so a bbox alone does not reduce bytes transferred). Not valid with --product statistics.",
)
@weather_skill.argument(
    "--statistic",
    choices=list(_STATISTICS),
    action="append",
    help="With --product statistics: restrict to these statistics (repeatable). Default: all six.",
)
@weather_skill.argument(
    "--billing-project",
    default=None,
    help="GCP project billed for requester-pays reads (WeatherNext 3 ensemble). "
    "Default: $GOOGLE_CLOUD_PROJECT, then $CLOUDSDK_CORE_PROJECT.",
)
@weather_skill.argument(
    "--max-lead",
    type=int,
    default=None,
    metavar="HOURS",
    help="Keep only leads up to this many hours after init. Applied before download — "
    "store chunks are one global field per lead, so this (with -v and --member) is what "
    "cuts read size for a short-range fetch.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help="Print the latest available YYYY-MM-DD on stdout and exit. Does not download fields.",
)
def fetch(date, bbox, variable, output, member=None, **kwargs):
    """Fetch a Google WeatherNext 2 or 3 forecast and write a weather-skills standard dataset.

    Opens one per-init consolidated Zarr from the chosen realtime archive with
    Google Cloud Application Default Credentials, maps dims onto the standard
    ensemble-forecast cube (WeatherNext 3 leads flattened to one hourly
    ``step``), optionally subsets by ``--bbox`` / ``--variable``, and returns a
    Dataset for the decorator to write.
    """
    product = _resolve_product(kwargs.get("model_version") or "2", kwargs.get("product"))
    cycle = kwargs.get("cycle")
    statistic = kwargs.get("statistic")
    billing_project = _billing_project(kwargs.get("billing_project"))
    _check_cycle(product, cycle)
    if member and not product.ensemble:
        raise UsageError("--member needs ensemble members; --product statistics has none.")
    if statistic and product.ensemble:
        raise UsageError("--statistic is only valid with --version 3 --product statistics.")

    if kwargs.get("probe_latest") is not None:
        stamp = _latest_stamp(product, cycle, billing_project)
        print(_stamp_to_date(stamp) if stamp else "none")
        return

    stamp = _resolve_stamp(date, cycle, product, billing_project)
    print(f"Resolved init: {stamp}", file=sys.stderr)
    print(f"Opening {_store_url(stamp, product)}", file=sys.stderr)

    ds = _open_remote(stamp, product, billing_project)
    if member:
        n = ds.sizes["sample"]
        bad = [m for m in member if m < -n or m >= n]
        if bad:
            raise UsageError(f"--member {bad} out of range for {n} members (valid: {-n}..{n - 1})")
        ds = ds.isel(sample=[m % n for m in member])
    ds = _prepare_dataset(ds, product, variable, statistic)
    max_lead = kwargs.get("max_lead")
    if max_lead is not None:
        import numpy as np

        keep = ds["step"].values <= np.timedelta64(max_lead, "h")
        if not keep.any():
            first = int(ds["step"].values[0] / np.timedelta64(1, "h"))
            raise UsageError(f"--max-lead {max_lead} keeps no leads (first lead is {first}h).")
        ds = ds.isel(step=np.flatnonzero(keep))
    if not variable:
        print(
            f"Note: no --variable given; selecting all data variables. {_size_note(product)}",
            file=sys.stderr,
        )
    if bbox is not None:
        ds = bbox_subset(ds, bbox)
    else:
        ds = ensure_normalized_longitude(ds)

    ds = ds.load()
    ds.attrs.update(
        Conventions="CF-1.13",
        title=product.title,
        institution="Google DeepMind / Google Research (WeatherNext)",
        source=_store_url(stamp, product),
        weather_skills_source=f"{product.source_tag}:{stamp}",
    )
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval=product.precip_interval, deaccumulate=False)
    ds = _shift_step_origin_to_zero(ds)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()

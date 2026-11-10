# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "dynamical-catalog==1.0.1",
#   "pandas",
#   "gcsfs",
# ]
# ///
"""Fetch a Google WeatherNext 2 or 3 forecast for one init date or a range of inits as a weather-skills Zarr."""

from __future__ import annotations

import json
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
_SKILL_VERSION = "0.0.10"

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

    version: str
    product: str
    bucket: str
    prefix: str
    metadata_file: str  # consolidated-metadata object that marks a complete store
    requester_pays: bool
    cycles: tuple[str, ...]
    precip_interval: str
    title: str
    source_tag: str
    size_note: str

    @property
    def ensemble(self) -> bool:
        return self.product == "ensemble"


_PRODUCTS = {
    ("2", "ensemble"): _Product(
        version="2",
        product="ensemble",
        bucket="weathernext",
        prefix="weathernext_2_0_0/zarr/2025_to_present",
        metadata_file=".zmetadata",
        requester_pays=False,
        cycles=_SYNOPTIC_CYCLES,
        precip_interval="6 hour",
        title="Google WeatherNext 2 ensemble forecast",
        source_tag="weathernext",
        size_note="WeatherNext 2 is 64 members x 60 leads x 0.25deg global, chunked per "
        "member/lead (a --bbox does not shrink the download) — pass -v and --member to "
        "keep the read small.",
    ),
    ("3", "ensemble"): _Product(
        version="3",
        product="ensemble",
        bucket="weathernext3_spatial",
        prefix="weathernext_3_0_0/zarr/2026_to_present",
        metadata_file="zarr.json",
        requester_pays=True,
        cycles=_ALL_CYCLES,
        precip_interval="1 hour",
        title="Google WeatherNext 3 ensemble forecast",
        source_tag="weathernext3",
        size_note="WeatherNext 3 is 64 members x 360 hourly leads x 0.1deg global, chunked "
        "per member per 6h lead block (a --bbox does not shrink the download, and the bucket "
        "is requester pays) — pass -v and --member to keep the read small. Defaulted to the "
        "0.1deg hourly surface fields.",
    ),
    ("3", "statistics"): _Product(
        version="3",
        product="statistics",
        bucket="weathernext3_statistics_spatial",
        prefix="weathernext_3_0_0_statistics/zarr/2026_to_present",
        metadata_file="zarr.json",
        requester_pays=False,
        cycles=_ALL_CYCLES,
        precip_interval="1 hour",
        title="Google WeatherNext 3 ensemble statistics forecast",
        source_tag="weathernext3-statistics",
        size_note="WeatherNext 3 statistics are 360 hourly leads x 0.1deg global, chunked per "
        "lead (a --bbox does not shrink the download) — pass -v and --statistic to keep the "
        "read small. Defaulted to the 0.1deg surface fields.",
    ),
}

_V2_NATIVE_PRECIP = "total_precipitation_6hr"

# dynamical.org copies of the WeatherNext 2 stores: 2025 on (operational) and
# 2022-2024 (historical), which gs://weathernext's realtime stores do not have.
_CATALOG_OPERATIONAL = "google-weathernext2-forecast-operational-virtual"
_CATALOG_HISTORICAL = "google-weathernext2-forecast-historical-virtual"
_GCS_V2_START = "20250101"
# dynamical.org copies of the WeatherNext 3 statistics store: 15-day stores for
# 00/06/12/18 UTC inits, 48-hour stores for every hourly init, each at 0.1deg and
# 0.05deg (the station-head fields). Catalog name -> GCS native name, before _<stat>.
_CATALOG_V3 = "google-weathernext3-forecast-{length}-{res}-degree-virtual"
_CATALOG_V3_STATION_HEAD = {
    "temperature_2m": "station_head_temperature_2m",
    "dew_point_temperature_2m": "station_head_dewpoint_temperature_2m",
}
_CATALOG_V3_VARS = {
    "precipitation_surface": "total_precipitation_1hr",
    "precipitation_imerg_surface": "imerg_tp_1hr",
    "precipitation_experimental_surface": "experimental_tp_1hr",
    "temperature_2m": "temperature_2m",
    "dew_point_temperature_2m": "dewpoint_temperature_2m",
    "wind_u_10m": "u_component_of_wind_10m",
    "wind_v_10m": "v_component_of_wind_10m",
    "wind_u_100m": "u_component_of_wind_100m",
    "wind_v_100m": "v_component_of_wind_100m",
    "wind_speed_10m": "wind_speed_10m",
    "wind_speed_100m": "wind_speed_100m",
    "pressure_reduced_to_mean_sea_level": "mean_sea_level_pressure",
    "sea_surface_temperature": "sea_surface_temperature",
    "total_cloud_cover_atmosphere": "total_cloud_cover",
    "cloud_cover_low": "low_cloud_cover",
    "cloud_cover_medium": "medium_cloud_cover",
    "cloud_cover_high": "high_cloud_cover",
    "downward_short_wave_radiation_flux_surface": "surface_solar_radiation_downwards_1hr",
    "downward_direct_short_wave_radiation_flux_surface": (
        "total_sky_direct_solar_radiation_at_surface_1hr"
    ),
}
_STAGING_CATALOG = "https://stac-staging.dynamical.org/catalog.json"
# Catalog name -> the gs://weathernext WeatherNext 2 native name.
_CATALOG_VARS = {
    "total_precipitation_surface": _V2_NATIVE_PRECIP,
    "temperature_2m": "2m_temperature",
    "sea_surface_temperature": "sea_surface_temperature",
    "pressure_reduced_to_mean_sea_level": "mean_sea_level_pressure",
    "wind_u_10m": "10m_u_component_of_wind",
    "wind_v_10m": "10m_v_component_of_wind",
    "wind_u_100m": "100m_u_component_of_wind",
    "wind_v_100m": "100m_v_component_of_wind",
}

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
    "total_precipitation_1hr": "precip",
    "experimental_tp_1hr": "experimental_precip",
    "imerg_tp_1hr": "imerg_precip",
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

_PRECIP_LONG_NAMES = {
    "precip": "Total precipitation",
    "experimental_precip": "Experimental total precipitation",
    "imerg_precip": "IMERG-calibrated total precipitation",
}
# Period totals labelled at the end of their period: precipitation and the
# WeatherNext 3 1 h solar radiation sums (kept as J m-2 per hour, as other
# fetchers keep ssrd, not turned into a W m-2 flux).
_PERIOD_TOTALS = (
    *_PRECIP_LONG_NAMES,
    "surface_solar_radiation_downwards_1hr",
    "total_sky_direct_solar_radiation_at_surface_1hr",
)

# Aliases shared by both versions, resolved to the *output* name. v3 native
# names come from _V3_RENAME; lookups fall back to the name itself.
_VAR_ALIASES = {
    **_V3_RENAME,
    _V2_NATIVE_PRECIP: "precip",
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "u100": "100m_u_component_of_wind",
    "v100": "100m_v_component_of_wind",
    "si10": "10m_wind_speed",
    "si100": "100m_wind_speed",
    "msl": "mean_sea_level_pressure",
    "sst": "sea_surface_temperature",
    "tcc": "total_cloud_cover",
    "lcc": "low_cloud_cover",
    "mcc": "medium_cloud_cover",
    "hcc": "high_cloud_cover",
    "ssrd": "surface_solar_radiation_downwards_1hr",
    "tp": "precip",
    "precipitation": "precip",
    "total_precipitation": "precip",
    "pr": "precip",
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

# Output names the catalog can serve (``precip`` for its 6 h totals).
_CATALOG_FIELDS = {_VAR_ALIASES.get(n, n) for n in _CATALOG_VARS.values()}
_CATALOG_V3_FIELDS = {_VAR_ALIASES.get(n, n) for n in _CATALOG_V3_VARS.values()}
_CATALOG_V3_STATION_HEAD_FIELDS = set(_CATALOG_V3_STATION_HEAD.values())

_GRID_RE = re.compile(r"^lat_(0p\d+)$")
# Default WeatherNext 3 field group when no --variable is given: the hourly
# 0.1deg surface fields (every field but station-head 0.05deg and 0.25deg upper air).
_V3_DEFAULT_GRID = "0p1"


_REQUESTER_PAYS_HINT = (
    "pass --billing-project PROJECT_ID (or set GOOGLE_CLOUD_PROJECT); reads are billed "
    "to that project."
)
_AUTH_TOKENS = (
    "401",
    "403",
    "forbidden",
    "credential",
    "anonymous",
    "requester pays",
    "user project",
)


def _auth_msg(product: _Product) -> str:
    msg = (
        f"cannot read gs://{product.bucket} (requires an authenticated Google Cloud "
        "identity, not anonymous access). Set GCLOUD_SERVICE_CREDENTIALS to the "
        "full service-account key JSON (no file needed), GOOGLE_APPLICATION_CREDENTIALS "
        "to a service-account JSON file path, or run `gcloud auth application-default login`."
    )
    if product.requester_pays:
        msg += f" This bucket is requester pays: {_REQUESTER_PAYS_HINT}"
    return msg


def _resolve_product(version: str, product_name: str | None) -> _Product:
    name = product_name or "ensemble"
    if version == "2" and name != "ensemble":
        raise UsageError("--product statistics is WeatherNext 3 only (use --version 3).")
    return _PRODUCTS[version, name]


def _billing_project(explicit: str | None) -> str | None:
    return (
        explicit
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("CLOUDSDK_CORE_PROJECT")
    )


def _filesystem(product: _Product, billing_project: str | None = None):
    import gcsfs

    kwargs = {"requester_pays": billing_project} if product.requester_pays else {}
    creds_json = os.environ.get("GCLOUD_SERVICE_CREDENTIALS")
    if creds_json:
        try:
            info = json.loads(creds_json)
        except json.JSONDecodeError as exc:
            raise DataError(f"GCLOUD_SERVICE_CREDENTIALS is not valid JSON ({exc}).") from None
        if not isinstance(info, dict):
            raise DataError("GCLOUD_SERVICE_CREDENTIALS must be a service-account key JSON object.")
        kwargs["token"] = info
    try:
        return gcsfs.GCSFileSystem(**kwargs)
    except Exception as exc:  # noqa: BLE001 — surface ADC failures as DataError
        raise DataError(f"{_auth_msg(product)} ({type(exc).__name__}: {exc})") from None


def _gcs_error(exc: Exception, what: str, product: _Product) -> DataError:
    text = f"{type(exc).__name__}: {exc}"
    hint = ""
    lowered = text.lower()
    if any(token in lowered for token in _AUTH_TOKENS):
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
            f"--cycle {cycle} is not published for WeatherNext {product.version} "
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
                    "; WeatherNext 3 also has 48h interim inits at the other (non-00/06/12/18) hours"
                    if product.version == "3"
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
        # Lazy without dask: indexing stays lazy, so only the chunks left after the
        # --member/--variable/--max-lead/--bbox subset are read at load().
        return xr.open_zarr(fs.get_mapper(url), consolidated=True, chunks=None)
    except (DataError, UsageError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"failed to open remote store {url}", product) from None


def _catalog_id(stamp: str, product: _Product, grid: str = "0p1") -> str:
    if product.version == "3":
        length = "15-day" if stamp[9:11] in _SYNOPTIC_CYCLES else "48-hour"
        return _CATALOG_V3.format(length=length, res=grid.replace("p", "-"))
    return _CATALOG_HISTORICAL if stamp[:8] < _GCS_V2_START else _CATALOG_OPERATIONAL


def _open_catalog(dataset: str):
    """Open a dynamical.org WeatherNext store lazily, in its GCS store's layout."""
    import os

    import dynamical_catalog

    if dataset not in dynamical_catalog.list():
        os.environ["DYNAMICAL_STAC_CATALOG_URL"] = _STAGING_CATALOG
        dynamical_catalog.clear_cache()
        if dataset not in dynamical_catalog.list():
            raise DataError(f"{dataset} is not in the dynamical.org catalog.")
    ds = dynamical_catalog.open(dataset, chunks=None)
    if "weathernext3" in dataset:
        import xarray as xr

        grid = "0p05" if "0-05-degree" in dataset else "0p1"
        names = _CATALOG_V3_STATION_HEAD if grid == "0p05" else _CATALOG_V3_VARS
        # One variable per field and statistic (temperature_2m_p90), as on GCS.
        # Lazy selections: only the statistics picked later are read at load().
        stats = [str(s) for s in ds["statistic"].values]
        ds = ds.drop_vars([c for c in ds.coords if c not in ds.dims and c != "init_time"])
        # GCS holds float32 lat/lon; matching it keeps the bbox and longitude arithmetic identical.
        ds = ds.assign_coords(y=ds["y"].astype("float32"), x=ds["x"].astype("float32"))
        return xr.Dataset(
            {
                f"{names[name]}_{stat}": ds[name].sel(statistic=stat, drop=True)
                for name in ds.data_vars
                if name in names
                for stat in stats
            }
        ).rename({"y": f"lat_{grid}", "x": f"lon_{grid}"})
    ds = ds.drop_vars(["expected_forecast_length", "spatial_ref"], errors="ignore")
    ds = ds.rename(
        {
            "ensemble_member": "sample",
            "lead_time": "time",
            "valid_time": "datetime",
            "y": "lat",
            "x": "lon",
            **{k: v for k, v in _CATALOG_VARS.items() if k in ds.data_vars},
        }
    )
    return ds.assign_coords(sample=ds["sample"].values.astype("int64"))


def _catalog_inits(stamps, product: _Product, latest=False, grid="0p1"):
    """dynamical.org inits for these init stamps, as {stamp: lazy cube}.

    With ``latest`` (stamps newest first) only the first complete init is kept.
    The catalog lists an init while it still publishes; such an init, or one
    the catalog lacks, is skipped with a warning (never filled).
    """
    import numpy as np

    opened, out = {}, {}
    for stamp in stamps:
        day, hour = stamp[:8], stamp[9:11]
        dataset = _catalog_id(stamp, product, grid)
        if dataset not in opened:
            opened[dataset] = _open_catalog(dataset)
        cat = opened[dataset]
        init = np.datetime64(f"{day[:4]}-{day[4:6]}-{day[6:]}T{hour}:00", "ns")
        if init not in cat["init_time"].values:
            if not latest:
                print(f"WARNING: dynamical.org has no init {stamp}; skipped.", file=sys.stderr)
            continue
        one = cat.sel(init_time=init)
        # One value of the last lead (t2m is defined everywhere) says the init is complete.
        if product.version == "2":
            t2m = one["2m_temperature"]
        else:
            t2m = one[
                "temperature_2m_mean" if grid == "0p1" else "station_head_temperature_2m_mean"
            ]
        last = t2m.isel({d: -1 for d in t2m.dims}).values
        if np.isnan(float(last)):
            print(
                f"WARNING: dynamical.org init {stamp} is still publishing; skipped.",
                file=sys.stderr,
            )
            continue
        out[stamp] = one
        if latest:
            break
    return out


def _latest_candidates(cycle: str | None) -> list[str]:
    """Init stamps of the last days, newest first, for ``cycle`` (default 00/06/12/18)."""
    import datetime as dt

    today = dt.datetime.now(dt.UTC).date()
    return [
        f"{(today - dt.timedelta(days=n)).strftime('%Y%m%d')}_{hour}hr_01_preds"
        for n in range(_LATEST_LOOKBACK_DAYS)
        for hour in reversed((cycle,) if cycle else _SYNOPTIC_CYCLES)
    ]


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
    return f"{grid.replace('p', '.')}deg {'hourly' if hourly else '6-hourly'}"


def _resolve_names(variable, available) -> list[str]:
    """``--variable`` values → deduped names in ``available``; UsageError lists misses."""
    resolved, missing = [], []
    for raw in variable:
        name = _VAR_ALIASES.get(raw, raw)
        if name not in available:
            missing.append(raw)
        elif name not in resolved:
            resolved.append(name)
    if missing:
        raise UsageError(
            f"variable(s) not in this WeatherNext product: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(available))}"
        )
    return resolved


def _select_v2(ds, variable):
    if _V2_NATIVE_PRECIP in ds.data_vars:
        ds = ds.rename({_V2_NATIVE_PRECIP: "precip"})
    return ds[_resolve_names(variable, ds.data_vars)] if variable else ds


def _select_v3(ds, variable, statistic):
    """Pick WeatherNext 3 fields by output name, renamed, all on one grid and lead axis."""
    out_names = {n: _v3_output_name(n) for n in ds.data_vars}
    by_base: dict[str, list[str]] = {}
    for native, out in out_names.items():
        by_base.setdefault(_split_statistic(out)[0], []).append(native)

    # Base output name -> the statistics a -v named for it (None: every one).
    picked: dict[str, set[str] | None] = {}
    if variable:
        missing = []
        for raw in variable:
            base, stat = _split_statistic(raw)
            base = _VAR_ALIASES.get(base, base)
            if base not in by_base or (
                stat and not any(_split_statistic(n)[1] == stat for n in by_base[base])
            ):
                missing.append(raw)
                continue
            if stat and statistic and stat not in statistic:
                raise UsageError(
                    f"-v {raw} names statistic {stat}, but --statistic keeps only "
                    f"{', '.join(statistic)}."
                )
            if stat is None or picked.get(base, set()) is None:
                picked[base] = None
            else:
                picked.setdefault(base, set()).add(stat)
        if missing:
            raise UsageError(
                f"variable(s) not in this WeatherNext product: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(by_base))}"
            )
    else:
        for b, names in by_base.items():
            if _field_group(ds[names[0]])[0] == _V3_DEFAULT_GRID:
                picked[b] = None

    natives = [
        n
        for b, stats in picked.items()
        for n in by_base[b]
        if stats is None or _split_statistic(n)[1] in stats
    ]
    if statistic:
        natives = [n for n in natives if _split_statistic(n)[1] in statistic]

    groups: dict[tuple, set[str]] = {}
    for native in natives:
        groups.setdefault(_field_group(ds[native]), set()).add(
            _split_statistic(out_names[native])[0]
        )
    if len(groups) > 1:
        detail = "; ".join(
            f"{_group_label(g)}: {', '.join(sorted(names))}" for g, names in groups.items()
        )
        raise UsageError(
            "WeatherNext 3 stores these variables on different grids or lead axes, so they "
            f"cannot share one dataset — fetch each group separately ({detail})."
        )

    grid = next(iter(groups))[0] if groups else None
    ds = ds[natives].rename({n: out_names[n] for n in natives})
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


def _prepare_dataset(ds, product: _Product):
    """Loaded native WeatherNext cube → standard ensemble-forecast dims and attrs."""
    import numpy as np

    if product.version == "3":
        ds = ds.drop_vars("datetime", errors="ignore")
        ds = _flatten_leads(ds)
        rename = {"sample": "number", "init_time": "time"}
    else:
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
        if base in _PRECIP_LONG_NAMES:
            # standard_name is stamped later by stamp_precip_amounts.
            ds[name] = ds[name].clip(min=0)
            ds[name].attrs.update(units="m", long_name=_PRECIP_LONG_NAMES[base])
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


def _trim_leads(ds, product: _Product, max_lead):
    """Keep native leads up to ``max_lead`` (timedelta64), before anything is read.

    WeatherNext 3 hourly fields sit in 6 h ``lead_time`` blocks; a block is kept
    when its first hour is in range, and the hours past ``max_lead`` are cut
    after the leads are flattened.
    """
    import numpy as np

    if product.version == "2":
        dim = "time"
        lead = np.asarray(ds["time"].values).astype("timedelta64[ns]")
    else:
        dim = "lead_time"
        lead = _as_hours(ds["lead_time"].values)
        if "lead_subtime" in ds.dims:
            lead = lead + _as_hours(ds["lead_subtime"].values).min()
    keep = lead <= max_lead
    if not keep.any():
        first = int(lead[0] / np.timedelta64(1, "h"))
        limit = max_lead / np.timedelta64(1, "h")
        raise UsageError(f"--max-lead {limit:g}h keeps no leads (first lead is {first}h).")
    return ds.isel({dim: np.flatnonzero(keep)})


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
    help="WeatherNext 3 only. ensemble (default): 64 members, requester pays. "
    "statistics: precomputed ensemble mean and p10/p25/p50/p75/p90, no members, free to read.",
)
@weather_skill.argument("--date")
@weather_skill.argument(
    "--start-time",
    action="append",
    help="Init window start; repeat with --end-time for several windows (each January).",
)
@weather_skill.argument(
    "--end-time", action="append", help="Init window end, paired with --start-time."
)
@weather_skill.argument(
    "--cycle",
    type=lambda hour: hour.zfill(2),
    choices=list(_ALL_CYCLES),
    help="Init hour UTC (6 and 06 both work). Version 2: 00/06/12/18. Version 3 adds 48h "
    "interim inits at the other (non-00/06/12/18) hours. Default: 00 with --date or windows; "
    "the latest 00/06/12/18 init when neither is given.",
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
    help="GCP project billed for requester-pays reads (WeatherNext 3 ensemble). "
    "Default: $GOOGLE_CLOUD_PROJECT, then $CLOUDSDK_CORE_PROJECT.",
)
@weather_skill.argument(
    "--max-lead",
    help="Keep only leads up to this one after init: hours (72) or a timedelta (72h, 3D). "
    "Applied before download — "
    "store chunks are one global field per lead, so this (with -v and --member) is what "
    "cuts read size for a short-range fetch.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help="Print the latest available YYYY-MM-DD on stdout and exit. Does not download fields.",
)
def fetch(
    date,
    start_time,
    end_time,
    bbox,
    variable,
    member,
    cycle,
    model_version,
    product,
    statistic,
    billing_project,
    max_lead,
    probe_latest,
    **kwargs,
):
    """Fetch a Google WeatherNext 2 or 3 forecast for one init date or a range of inits as a weather-skills Zarr."""
    # Opens one per-init consolidated Zarr from the chosen realtime archive with
    # Google Cloud Application Default Credentials, maps dims onto the standard
    # ensemble-forecast cube (WeatherNext 3 leads flattened to one hourly
    # ``step``), optionally subsets by ``--bbox`` / ``--variable``, and returns a
    # Dataset for the decorator to write.
    import numpy as np

    product = _resolve_product(model_version, product)
    billing_project = _billing_project(billing_project)
    _check_cycle(product, cycle)
    if product.requester_pays and not billing_project:
        raise UsageError(f"gs://{product.bucket} is requester pays: {_REQUESTER_PAYS_HINT}")
    if member and not product.ensemble:
        raise UsageError("--member needs ensemble members; --product statistics has none.")
    if statistic and product.ensemble:
        raise UsageError("--statistic is only valid with --version 3 --product statistics.")
    if max_lead is not None:
        import pandas as pd

        text = max_lead.strip()
        try:
            lead = pd.Timedelta(int(text), "h") if text.isdigit() else pd.Timedelta(text)
        except ValueError:
            raise UsageError(f"--max-lead {max_lead!r} is not a lead (72, 72h, 3D).") from None
        if lead < pd.Timedelta(0):
            raise UsageError("--max-lead must not be negative.")
        max_lead = lead.to_timedelta64().astype("timedelta64[ns]")

    if probe_latest is not None:
        stamp = None
        if product.version == "2" or not product.ensemble:
            try:
                candidates = _latest_candidates(cycle)
                stamp = next(iter(_catalog_inits(candidates, product, latest=True)), None)
            except Exception as exc:  # noqa: BLE001 — any catalog failure falls back to GCS
                print(f"dynamical.org catalog failed ({exc}); probing GCS.", file=sys.stderr)
        if stamp is None:
            stamp = _latest_stamp(product, cycle, billing_project)
        print(_stamp_to_date(stamp) if stamp else "none")
        return

    # One (start, end) pair per window; --end-time is an inclusive calendar day.
    windows = list(zip(start_time or [], end_time or [], strict=True))
    if date is not None and windows:
        raise UsageError(
            "pass --date for one init, or --start-time/--end-time for a range of inits (not both)."
        )
    # Init days (YYYYMMDD); None takes the latest init. The windows select init
    # times, not valid times: the --cycle init (default 00 UTC) of each day.
    days = None
    if windows:
        import datetime as dt

        days = sorted(
            {
                (start + dt.timedelta(days=n)).strftime("%Y%m%d")
                for start, end in windows
                for n in range((end - start).days + 1)
            }
        )
    elif date is not None:
        days = [date.strftime("%Y%m%d")]

    # WeatherNext 2 surface fields and WeatherNext 3 statistics come from
    # dynamical.org (no credentials) when it has the fields and inits; anything
    # else from GCS.
    catalog = None
    grid = "0p1"
    if product.version == "2" or not product.ensemble:
        historical = bool(days) and days[0] < _GCS_V2_START
        if product.version == "2":
            # No -v means every field, pressure levels included: GCS.
            fields = _CATALOG_FIELDS
            fits = bool(variable) and all(_VAR_ALIASES.get(v, v) in fields for v in variable)
        else:
            # No -v means the 0.1deg surface fields, which the catalog has. One
            # catalog store per grid, as one run reads one grid.
            fields = _CATALOG_V3_FIELDS | _CATALOG_V3_STATION_HEAD_FIELDS
            bases = {
                _VAR_ALIASES.get(b, b) for b in (_split_statistic(v)[0] for v in variable or [])
            }
            if bases and bases <= _CATALOG_V3_STATION_HEAD_FIELDS:
                grid = "0p05"
            fits = bases <= _CATALOG_V3_FIELDS or grid == "0p05"
        gcs = f"gs://{product.bucket}/{product.prefix}/"
        if not fits:
            why = "dynamical.org has only " + ", ".join(sorted(fields))
            if historical:
                raise DataError(f"WeatherNext 2 inits before 2025 are not on {gcs}; {why} (-v).")
        else:
            try:
                if days is None:
                    candidates = _latest_candidates(cycle)
                    catalog = _catalog_inits(candidates, product, latest=True, grid=grid)
                else:
                    hour = cycle or "00"
                    stamps = [f"{day}_{hour}hr_01_preds" for day in days]
                    catalog = _catalog_inits(stamps, product, grid=grid)
                if not catalog:
                    raise DataError("no complete init for the requested dates")
            except Exception as exc:  # noqa: BLE001 — any catalog failure falls back to GCS
                why = f"dynamical.org failed: {exc}"
                if historical:
                    raise DataError(
                        f"{why}. WeatherNext 2 inits before 2025 are not on {gcs}."
                    ) from None
                catalog = None
        if catalog is not None:
            ids = sorted({_catalog_id(s, product, grid) for s in catalog})
            print(f"Source: dynamical.org {', '.join(ids)}", file=sys.stderr)
        else:
            print(f"Source: {gcs} ({why})", file=sys.stderr)

    if catalog is not None:
        stamps = sorted(catalog)
        if windows:
            print(f"Selected {len(stamps)} inits.", file=sys.stderr)
        else:
            print(f"Resolved init: {stamps[0]}", file=sys.stderr)
    elif windows:
        # One listing per month in the windows.
        hour = cycle or "00"
        stamps = [
            stamp
            for month in sorted({day[:6] for day in days})
            for stamp in _list_init_stamps(product, billing_project, month)
            if stamp[:8] in days and _STAMP_RE.fullmatch(stamp).group(2) == hour
        ]
        if not stamps:
            window_text = ", ".join(f"{start}..{end}" for start, end in windows)
            raise DataError(
                f"no {hour} UTC init in {window_text} under gs://{product.bucket}/{product.prefix}/."
            )
        print(f"Selected {len(stamps)} inits.", file=sys.stderr)
    else:
        stamps = [_resolve_stamp(date, cycle, product, billing_project)]
        print(f"Resolved init: {stamps[0]}", file=sys.stderr)
    if not variable:
        print(
            f"Note: no --variable given; selecting all data variables. {product.size_note}",
            file=sys.stderr,
        )

    # One store per init: open each lazily, subset it, and load only that.
    pieces = []
    for stamp in stamps:
        if catalog is not None:
            ds = catalog[stamp]
        else:
            if windows and not _store_exists(stamp, product, billing_project):
                print(
                    f"WARNING: no complete store for init {stamp} (still publishing?); skipped.",
                    file=sys.stderr,
                )
                continue
            print(f"Opening {_store_url(stamp, product)}", file=sys.stderr)
            ds = _open_remote(stamp, product, billing_project)
        if member:
            n = ds.sizes["sample"]
            bad = [m for m in member if m < -n or m >= n]
            if bad:
                raise UsageError(
                    f"--member {bad} out of range for {n} members (valid: {-n}..{n - 1})"
                )
            ds = ds.isel(sample=[m % n for m in member])
        if product.version == "3":
            ds = _select_v3(ds, variable, statistic)
        else:
            ds = _select_v2(ds, variable)
        if max_lead is not None:
            ds = _trim_leads(ds, product, max_lead)
        if bbox is not None:
            ds = bbox_subset(ds, bbox)
        else:
            ds = ensure_normalized_longitude(ds)
        ds = ds.load()
        if catalog is not None:
            # The catalog's units back to the GCS stores' K, m, J m-2 and 0-1, no attrs.
            # WeatherNext 3 rates and fluxes are 1 h means; GCS holds 1 h totals.
            for name in ds.data_vars:
                units = ds[name].attrs.get("units")
                if units == "degree_Celsius":
                    ds[name] = ds[name] + np.float32(273.15)
                elif units == "kg m-2":
                    ds[name] = ds[name] / np.float32(1000)
                elif units == "kg m-2 s-1":
                    ds[name] = ds[name] * np.float32(3.6)
                elif units == "W m-2":
                    ds[name] = ds[name] * np.float32(3600)
                elif units == "percent":
                    ds[name] = ds[name] / np.float32(100)
        # Native attrs (catalog CF units, GCS step_type and statistic labels) are
        # replaced by _prepare_dataset's, so every source writes the same attrs.
        ds = ds.drop_attrs()
        pieces.append(_prepare_dataset(ds, product))
    if not pieces:
        raise DataError("no complete store for any selected init.")
    if windows:
        import xarray as xr

        # A range of inits: a `time` axis (the init); valid_time becomes (time, step).
        ds = xr.concat(pieces, "time", join="outer", coords="different", compat="equals")
        ds = ds.transpose("time", "step", ...)
    else:
        ds = pieces[0]
    if max_lead is not None:
        ds = ds.isel(step=np.flatnonzero(ds["step"].values <= max_lead))
    label = stamps[0] + (f"..{stamps[-1]}" if windows else "")
    ds.attrs.update(
        Conventions="CF-1.13",
        title=product.title,
        institution="Google DeepMind / Google Research (WeatherNext)",
        source=f"dynamical.org:{_catalog_id(stamps[0], product, grid)}"
        if catalog is not None
        else _store_url(stamps[0], product)
        if not windows
        else f"gs://{product.bucket}/{product.prefix}/",
        weather_skills_source=f"{product.source_tag}:{label}",
    )
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval=product.precip_interval, deaccumulate=False)
    # The rate conversion upcasts to float64; the stores are float32.
    for name in ds.data_vars:
        if _split_statistic(name)[0] in _PRECIP_LONG_NAMES:
            ds[name] = ds[name].astype("float32")
    # Precipitation and solar radiation are period totals labelled at their end:
    # relabel them to the period start. Every other field is instantaneous at
    # init + step and keeps its lead. With both, the merged axis runs from 0: the
    # totals are NaN at the last lead and the instantaneous fields at 0 (gaps,
    # not filled).
    totals = [n for n in ds.data_vars if _split_statistic(n)[0] in _PERIOD_TOTALS]
    if totals:
        import xarray as xr

        had_valid = "valid_time" in ds.coords
        ds = ds.drop_vars("valid_time", errors="ignore")
        shifted = _shift_step_origin_to_zero(ds[totals])
        rest = ds.drop_vars(totals)
        ds = (
            xr.merge([rest, shifted], join="outer", compat="override")
            if rest.data_vars
            else shifted
        )
        if had_valid:
            ds = ds.assign_coords(valid_time=ds["time"] + ds["step"])
    if windows:
        # An init still publishing its long leads keeps them NaN (not filled);
        # say which init is short.
        init_empty = np.ones((ds.sizes["time"], ds.sizes["step"]), dtype=bool)
        for name in ds.data_vars:
            other = [d for d in ds[name].dims if d not in ("time", "step")]
            init_empty &= ds[name].isnull().all(other).transpose("time", "step").values
        hours = ds["step"].values / np.timedelta64(1, "h")
        for i, row in enumerate(init_empty):
            init = str(ds["time"].values[i])[:13]
            if row.all():
                print(f"WARNING: WeatherNext {init} UTC init has no data (NaN).", file=sys.stderr)
            elif row[-1]:
                print(
                    f"WARNING: WeatherNext {init} UTC init has no data after step "
                    f"{hours[np.nonzero(~row)[0][-1]]:g} h (still publishing?); its later "
                    "steps are NaN (not filled).",
                    file=sys.stderr,
                )
    return stamp_data_interval(ds, dim="step" if "step" in ds.dims else None)


if __name__ == "__main__":
    fetch()

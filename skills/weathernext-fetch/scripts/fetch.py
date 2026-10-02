# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "xarray",
#   "zarr>=3",
#   "cftime",
#   "gcsfs",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a Google WeatherNext 2 ensemble forecast and write a weather-skills standard dataset."""

from __future__ import annotations

import re
import sys

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

_BUCKET = "weathernext"
_PREFIX = "weathernext_2_0_0/zarr/2025_to_present"
_STORE_NAME = "predictions.zarr"
_STAMP_RE = re.compile(r"^(\d{8})_(\d{2})hr_01_preds$")
_CYCLES = ("00", "06", "12", "18")
_PRECIP_INTERVAL = "6 hour"

_NATIVE_PRECIP = "total_precipitation_6hr"

# Native variable -> (units, standard_name, long_name). Source Zarr carries no
# attrs at all, so every field is stamped here (values match ARCO-ERA5's own
# metadata for the same ERA5-lineage fields, for cross-source consistency).
_NATIVE_ATTRS = {
    "2m_temperature": ("K", "air_temperature", "2 metre temperature"),
    "10m_u_component_of_wind": ("m s-1", "eastward_wind", "10 metre U wind component"),
    "10m_v_component_of_wind": ("m s-1", "northward_wind", "10 metre V wind component"),
    "100m_u_component_of_wind": ("m s-1", "eastward_wind", "100 metre U wind component"),
    "100m_v_component_of_wind": ("m s-1", "northward_wind", "100 metre V wind component"),
    "mean_sea_level_pressure": ("Pa", "air_pressure_at_mean_sea_level", "Mean sea level pressure"),
    "sea_surface_temperature": ("K", "sea_surface_temperature", "Sea surface temperature"),
    "specific_humidity": ("kg kg-1", "specific_humidity", "Specific humidity"),
    "temperature": ("K", "air_temperature", "Temperature"),
    "u_component_of_wind": ("m s-1", "eastward_wind", "U component of wind"),
    "v_component_of_wind": ("m s-1", "northward_wind", "V component of wind"),
    "vertical_velocity": ("Pa s-1", "lagrangian_tendency_of_air_pressure", "Vertical velocity"),
    "geopotential": ("m2 s-2", "geopotential", "Geopotential"),
}

_VAR_ALIASES = {
    "t2m": "2m_temperature",
    "2m_temperature": "2m_temperature",
    "u10": "10m_u_component_of_wind",
    "10m_u_component_of_wind": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "10m_v_component_of_wind": "10m_v_component_of_wind",
    "u100": "100m_u_component_of_wind",
    "100m_u_component_of_wind": "100m_u_component_of_wind",
    "v100": "100m_v_component_of_wind",
    "100m_v_component_of_wind": "100m_v_component_of_wind",
    "msl": "mean_sea_level_pressure",
    "mean_sea_level_pressure": "mean_sea_level_pressure",
    "sst": "sea_surface_temperature",
    "sea_surface_temperature": "sea_surface_temperature",
    "tp": "tp",
    "precip": "tp",
    "precipitation": "tp",
    "total_precipitation": "tp",
    _NATIVE_PRECIP: "tp",
    "pr": "tp",
    "q": "specific_humidity",
    "specific_humidity": "specific_humidity",
    "t": "temperature",
    "temperature": "temperature",
    "u": "u_component_of_wind",
    "u_component_of_wind": "u_component_of_wind",
    "v": "v_component_of_wind",
    "v_component_of_wind": "v_component_of_wind",
    "w": "vertical_velocity",
    "vertical_velocity": "vertical_velocity",
    "z": "geopotential",
    "geopotential": "geopotential",
}

_AUTH_MSG = (
    "cannot read gs://weathernext (requires an authenticated Google Cloud identity, "
    "not anonymous access). Set GOOGLE_APPLICATION_CREDENTIALS to a service-account "
    "JSON, or run `gcloud auth application-default login`."
)


def _filesystem():
    import gcsfs

    try:
        return gcsfs.GCSFileSystem()
    except Exception as exc:  # noqa: BLE001 — surface ADC failures as DataError
        raise DataError(f"{_AUTH_MSG} ({type(exc).__name__}: {exc})") from None


def _gcs_error(exc: Exception, what: str) -> DataError:
    text = f"{type(exc).__name__}: {exc}"
    hint = ""
    lowered = text.lower()
    if any(token in lowered for token in ("401", "403", "forbidden", "credential", "anonymous")):
        hint = f" {_AUTH_MSG}"
    return DataError(f"{what} ({text}).{hint}")


def _store_url(stamp: str) -> str:
    return f"gs://{_BUCKET}/{_PREFIX}/{stamp}/{_STORE_NAME}"


def _list_init_stamps() -> list[str]:
    fs = _filesystem()
    prefix = f"{_BUCKET}/{_PREFIX}"
    try:
        items = fs.ls(prefix, detail=False)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"GCS listing failed for gs://{prefix}") from None
    stamps = []
    for item in items:
        name = str(item).rstrip("/").rsplit("/", 1)[-1]
        if _STAMP_RE.fullmatch(name):
            stamps.append(name)
    stamps.sort()
    return stamps


def _store_exists(stamp: str) -> bool:
    fs = _filesystem()
    key = f"{_BUCKET}/{_PREFIX}/{stamp}/{_STORE_NAME}/.zmetadata"
    try:
        return bool(fs.exists(key))
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"GCS HEAD failed for gs://{key}") from None


def _stamp_to_date(stamp: str) -> str:
    match = _STAMP_RE.fullmatch(stamp)
    if not match:
        raise DataError(f"unrecognized WeatherNext init stamp {stamp!r}")
    compact = match.group(1)
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"


def _resolve_stamp(date, cycle: str | None) -> str:
    if date is not None:
        hour = cycle or "00"
        stamp = f"{date.strftime('%Y%m%d')}_{hour}hr_01_preds"
        if not _store_exists(stamp):
            raise DataError(
                f"no store at {_store_url(stamp)}. Pick a published init "
                "(`--probe-latest`), or omit --date to take the latest. Inits are "
                "00/06/12/18 UTC (`--cycle`). Only the 2025-to-present realtime "
                "archive is supported by this skill."
            )
        return stamp

    stamps = _list_init_stamps()
    if cycle:
        stamps = [s for s in stamps if s.endswith(f"_{cycle}hr_01_preds")]
    for stamp in reversed(stamps):
        if _store_exists(stamp):
            return stamp
    raise DataError(f"no predictions store under gs://{_BUCKET}/{_PREFIX}/.")


def _open_remote(stamp: str):
    import xarray as xr

    url = _store_url(stamp)
    try:
        fs = _filesystem()
        return xr.open_zarr(fs.get_mapper(url), consolidated=True)
    except DataError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"failed to open remote store {url}") from None


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


def _prepare_dataset(ds):
    """Native WeatherNext cube → standard ensemble-forecast dims and attrs."""
    import numpy as np

    rename = {"sample": "number", "time": "step", "datetime": "valid_time", "init_time": "time"}
    if _NATIVE_PRECIP in ds.data_vars:
        rename[_NATIVE_PRECIP] = "tp"
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
    if "time" in ds.coords:
        ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    if "valid_time" in ds.coords:
        ds["valid_time"].attrs.update(standard_name="time", long_name="valid time of forecast step")
    if "level" in ds.coords:
        ds["level"].attrs.update(
            standard_name="air_pressure",
            units="hPa",
            positive="down",
            axis="Z",
            long_name="pressure level",
        )

    for name, (units, standard_name, long_name) in _NATIVE_ATTRS.items():
        if name in ds.data_vars:
            ds[name].attrs.update(units=units, standard_name=standard_name, long_name=long_name)
    if "tp" in ds.data_vars:
        ds["tp"] = ds["tp"].clip(min=0)
        ds["tp"].attrs["units"] = "m"
        ds["tp"].attrs["standard_name"] = "lwe_thickness_of_precipitation_amount"
        ds["tp"].attrs["long_name"] = "Total precipitation"

    stamp_cf_attrs(ds)

    order = [d for d in ("number", "step", "level", "lat", "lon") if d in ds.dims]
    if order:
        ds = ds.transpose(*order)
    return ds


def _select_variables(ds, variable):
    if not variable:
        return ds
    resolved = []
    missing = []
    seen: set[str] = set()
    for raw in variable:
        name = _VAR_ALIASES.get(raw, raw)
        if name not in ds.data_vars:
            missing.append(raw)
            continue
        if name not in seen:
            resolved.append(name)
            seen.add(name)
    if missing:
        raise UsageError(
            f"variable(s) not in this WeatherNext product: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(ds.data_vars))}"
        )
    return ds[resolved]


@weather_skill(
    name="weathernext-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--date")
@weather_skill.argument(
    "--cycle",
    choices=list(_CYCLES),
    default=None,
    help="Init hour UTC (00, 06, 12, or 18). Default: 00 with --date; latest cycle "
    "when --date is omitted.",
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
    "so a bbox alone does not reduce bytes transferred).",
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
    """Fetch a Google WeatherNext 2 ensemble forecast and write a weather-skills standard dataset.

    Opens a consolidated Zarr under
    ``gs://weathernext/weathernext_2_0_0/zarr/2025_to_present/<init>/predictions.zarr``
    with Google Cloud Application Default Credentials, maps dims onto the
    standard ensemble-forecast cube, optionally subsets by ``--bbox`` /
    ``--variable``, and returns a Dataset for the decorator to write.
    """
    cycle = kwargs.get("cycle")
    if kwargs.get("probe_latest") is not None:
        stamps = _list_init_stamps()
        if cycle:
            stamps = [s for s in stamps if s.endswith(f"_{cycle}hr_01_preds")]
        for stamp in reversed(stamps):
            if _store_exists(stamp):
                print(_stamp_to_date(stamp))
                return
        print("none")
        return

    stamp = _resolve_stamp(date, cycle)
    print(f"Resolved init: {stamp}", file=sys.stderr)
    print(f"Opening {_store_url(stamp)}", file=sys.stderr)

    ds = _open_remote(stamp)
    ds = _prepare_dataset(ds)
    if member:
        n = ds.sizes["number"]
        bad = [m for m in member if m < -n or m >= n]
        if bad:
            raise UsageError(f"--member {bad} out of range for {n} members (valid: {-n}..{n - 1})")
        ds = ds.isel(number=[m % n for m in member])
    ds = _select_variables(ds, variable)
    if not variable:
        print(
            "Note: no --variable given; selecting all data variables. WeatherNext is "
            "64 members x 60 leads x 0.25deg global, chunked per member/lead (a --bbox "
            "does not shrink the download) — pass -v and --member to keep the read small.",
            file=sys.stderr,
        )
    if bbox is not None:
        ds = bbox_subset(ds, bbox)
    else:
        ds = ensure_normalized_longitude(ds)

    ds = ds.load()
    ds.attrs.update(
        Conventions="CF-1.13",
        title="Google WeatherNext 2 ensemble forecast",
        institution="Google DeepMind / Google Research (WeatherNext)",
        source=_store_url(stamp),
        weather_skills_source=f"weathernext:{stamp}",
    )
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval=_PRECIP_INTERVAL, deaccumulate=False)
    ds = _shift_step_origin_to_zero(ds)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()

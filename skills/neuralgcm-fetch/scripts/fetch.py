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
"""Fetch a NeuralGCM S2S ensemble forecast and write a weather-skills standard dataset."""

from __future__ import annotations

import re
import sys

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import (
    convert_dataarray,
    precip_amounts_to_rates,
    stamp_data_interval,
    stamp_precip_amounts,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_BUCKET = "neuralgcm-s2s"
_PREFIX = "staging/realtime/tomorrow_now_2026/v1"
_DEFAULT_DATASET = "imerg:precip"
_STAMP_RE = re.compile(r"^(\d{8})T(\d{2})$")
_CYCLES = ("00", "12")
_PRECIP_INTERVAL = "6 hour"

_NATIVE_PRECIP = "total_precipitation_6hr"
_NATIVE_T2M = "2m_temperature"
_NATIVE_D2M = "2m_dewpoint_temperature"

_DATASET_ALIASES = {
    "imerg:precip": "imerg:precip",
    "precip": "imerg:precip",
    "era5:surface": "era5:surface",
    "surface": "era5:surface",
}
_DATASET_CHOICES = list(_DATASET_ALIASES)

_VAR_RENAME = {
    _NATIVE_PRECIP: "tp",
    _NATIVE_T2M: "t2m",
    _NATIVE_D2M: "d2m",
}
_VAR_ALIASES = {
    "tp": "tp",
    "precip": "tp",
    "precipitation": "tp",
    "total_precipitation": "tp",
    _NATIVE_PRECIP: "tp",
    "pr": "tp",
    "t2m": "t2m",
    _NATIVE_T2M: "t2m",
    "d2m": "d2m",
    _NATIVE_D2M: "d2m",
}

_AUTH_MSG = (
    "cannot read gs://neuralgcm-s2s (private GCS). Set "
    "GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON that can read "
    "that bucket, or run `gcloud auth application-default login`."
)


def _canonical_dataset(dataset: str) -> str:
    key = (dataset or _DEFAULT_DATASET).strip()
    name = _DATASET_ALIASES.get(key, key)
    if name not in {"imerg:precip", "era5:surface"}:
        raise UsageError(
            f"unknown --dataset {dataset!r}; choose one of: {', '.join(_DATASET_CHOICES)}"
        )
    return name


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
        hint = (
            " Check GCS credentials: set GOOGLE_APPLICATION_CREDENTIALS or run "
            "`gcloud auth application-default login`."
        )
    return DataError(f"{what} ({text}).{hint}")


def _store_url(stamp: str, dataset: str) -> str:
    return f"gs://{_BUCKET}/{_PREFIX}/{stamp}/{dataset}"


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


def _store_exists(stamp: str, dataset: str) -> bool:
    fs = _filesystem()
    key = f"{_BUCKET}/{_PREFIX}/{stamp}/{dataset}/.zmetadata"
    try:
        return bool(fs.exists(key))
    except Exception as exc:  # noqa: BLE001
        raise _gcs_error(exc, f"GCS HEAD failed for gs://{key}") from None


def _stamp_to_date(stamp: str) -> str:
    match = _STAMP_RE.fullmatch(stamp)
    if not match:
        raise DataError(f"unrecognized NeuralGCM init stamp {stamp!r}")
    compact = match.group(1)
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"


def _resolve_stamp(date, cycle: str | None, dataset: str) -> str:
    if date is not None:
        hour = cycle or "00"
        stamp = f"{date.strftime('%Y%m%d')}T{hour}"
        if not _store_exists(stamp, dataset):
            raise DataError(
                f"no {dataset!r} store at {_store_url(stamp, dataset)}. "
                "Pick a published init (`--probe-latest`), or omit --date to take "
                "the latest. Inits are 00 and 12 UTC (`--cycle`)."
            )
        return stamp

    stamps = _list_init_stamps()
    if cycle:
        stamps = [s for s in stamps if s.endswith(f"T{cycle}")]
    for stamp in reversed(stamps):
        if _store_exists(stamp, dataset):
            return stamp
    raise DataError(
        f"no {dataset!r} store under gs://{_BUCKET}/{_PREFIX}/. "
        f"Available --dataset ids: {', '.join(_DATASET_CHOICES)}."
    )


def _open_remote(stamp: str, dataset: str):
    import xarray as xr

    url = _store_url(stamp, dataset)
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
    """Native NeuralGCM cube → standard forecast dims and variable names."""
    if "init_time" in ds.dims:
        if ds.sizes["init_time"] != 1:
            raise DataError(
                f"expected a single init_time in the per-init store; got {ds.sizes['init_time']}"
            )
        ds = ds.isel(init_time=0)
    rename = {}
    if "timedelta" in ds.dims or "timedelta" in ds.coords:
        rename["timedelta"] = "step"
    if "realization" in ds.dims or "realization" in ds.coords:
        rename["realization"] = "number"
    if "init_time" in ds.coords:
        rename["init_time"] = "time"
    if rename:
        ds = ds.rename(rename)
    present = {src: dst for src, dst in _VAR_RENAME.items() if src in ds.data_vars}
    if present:
        ds = ds.rename(present)
    if "tp" in ds.data_vars:
        ds["tp"] = ds["tp"].clip(min=0)
        ds["tp"].attrs["units"] = "m"
        ds["tp"].attrs["standard_name"] = "lwe_thickness_of_precipitation_amount"
        ds["tp"].attrs["long_name"] = "Total precipitation"
    if "t2m" in ds.data_vars:
        ds["t2m"].attrs.setdefault("units", "K")
        ds["t2m"].attrs.setdefault("standard_name", "air_temperature")
        ds["t2m"].attrs.setdefault("long_name", "2 metre temperature")
    if "d2m" in ds.data_vars:
        ds["d2m"].attrs.setdefault("units", "K")
        ds["d2m"].attrs.setdefault("standard_name", "dew_point_temperature")
        ds["d2m"].attrs.setdefault("long_name", "2 metre dewpoint temperature")
    for name in ("latitude", "longitude"):
        if name in ds.coords:
            ds[name].attrs.pop("lon_lat_padding", None)
    if "time" in ds.coords:
        ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    if "step" in ds.coords:
        ds["step"].attrs.update(
            standard_name="forecast_period",
            long_name="time since forecast_reference_time",
        )
    order = [d for d in ("number", "step", "latitude", "longitude") if d in ds.dims]
    others = [d for d in ds.dims if d not in order]
    if order:
        ds = ds.transpose(*others, *order)
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
            f"variable(s) not in this NeuralGCM product: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(ds.data_vars))}"
        )
    return ds[resolved]


@weather_skill(
    name="neuralgcm-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--dataset",
    default=_DEFAULT_DATASET,
    choices=_DATASET_CHOICES,
    help=(
        f"Realtime product under <init>/ (default {_DEFAULT_DATASET}; "
        "era5:surface is 2 m temperature and dewpoint)."
    ),
)
@weather_skill.argument("--date")
@weather_skill.argument(
    "--cycle",
    choices=list(_CYCLES),
    default=None,
    help="Init hour UTC (00 or 12). Default: 00 with --date; latest cycle when --date is omitted.",
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a --dataset id."
    ),
)
def fetch(dataset, date, bbox, variable, output, **kwargs):
    """Fetch a NeuralGCM S2S ensemble forecast and write a weather-skills standard dataset.

    Opens a consolidated Zarr under
    ``gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1/<init>/<dataset>/``
    with Google Cloud Application Default Credentials, maps dims onto the
    standard forecast cube, optionally subsets by ``--bbox`` / ``--variable``,
    and returns a Dataset for the decorator to write.
    """
    dsid = _canonical_dataset(kwargs["probe_latest"] or dataset)
    cycle = kwargs.get("cycle")
    if kwargs.get("probe_latest") is not None:
        stamps = _list_init_stamps()
        if cycle:
            stamps = [s for s in stamps if s.endswith(f"T{cycle}")]
        for stamp in reversed(stamps):
            if _store_exists(stamp, dsid):
                print(_stamp_to_date(stamp))
                return
        print("none")
        return

    dataset = _canonical_dataset(dataset)
    stamp = _resolve_stamp(date, cycle, dataset)
    print(f"Resolved init: {stamp}", file=sys.stderr)
    print(f"Opening {_store_url(stamp, dataset)}", file=sys.stderr)

    ds = _open_remote(stamp, dataset)
    ds = _prepare_dataset(ds)
    ds = _select_variables(ds, variable)
    if bbox is not None:
        ds = bbox_subset(ds, bbox)
    else:
        ds = ensure_normalized_longitude(ds)

    ds = ds.load()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"neuralgcm:{dataset}:{stamp}",
    )
    stamp_cf_attrs(ds)
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    if "d2m" in ds.data_vars and ds["d2m"].attrs.get("units") in {"K", "kelvin", "Kelvin"}:
        converted, _ = convert_dataarray(ds["d2m"], "degree_Celsius")
        ds["d2m"] = converted
    ds = precip_amounts_to_rates(ds, interval=_PRECIP_INTERVAL, deaccumulate=False)
    ds = _shift_step_origin_to_zero(ds)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()

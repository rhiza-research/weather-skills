# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "xarray",
#   "zarr>=3",
#   "cftime",
#   "adlfs",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a Cumulus AI operational ensemble forecast and write a weather-skills standard dataset."""

from __future__ import annotations

import re
import sys

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    require_env,
)
from weather_skills_core.units import (
    precip_amounts_to_rates,
    stamp_data_interval,
    stamp_precip_amounts,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_ACCOUNT = "italynorthdata"
_CONTAINER = "data"
_RUN_PREFIX = "live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9"
_ZARR_DIR = "zarr"
_SAS_ENV = "AZURE_STORAGE_SAS_TOKEN"
_DEFAULT_DATASET = "precip"
# The run publishes IMERG-trained precip at both native 1° and regridded
# 0.25°, as separate variable folders under zarr/. Default to 0.25°.
_NATIVE_PRECIP_0P25 = "total_precipitation_24h_acc_imerg_0p25"
_NATIVE_PRECIP_1DEG = "total_precipitation_24h_acc_imerg"
_NATIVE_PRECIP_ERA5 = "total_precipitation_24h_acc_era5"
_NATIVE_PRECIPS = (_NATIVE_PRECIP_0P25, _NATIVE_PRECIP_1DEG, _NATIVE_PRECIP_ERA5)
_NATIVE_PRECIP = _NATIVE_PRECIP_0P25
_OUTPUT_PRECIP = "tp"
DEFAULT_WORKERS = 8
_SIG_RE = re.compile(r"(sig=)[^&\s'\"<>]+", re.IGNORECASE)

# --dataset aliases → variable folder under .../zarr/<variable>/
_DATASET_ALIASES = {
    "precip": _NATIVE_PRECIP_0P25,
    "tp": _NATIVE_PRECIP_0P25,
    "total_precipitation": _NATIVE_PRECIP_0P25,
    "precip_0p25": _NATIVE_PRECIP_0P25,
    _NATIVE_PRECIP_0P25: _NATIVE_PRECIP_0P25,
    "precip_1deg": _NATIVE_PRECIP_1DEG,
    _NATIVE_PRECIP_1DEG: _NATIVE_PRECIP_1DEG,
    "precip_era5": _NATIVE_PRECIP_ERA5,
    _NATIVE_PRECIP_ERA5: _NATIVE_PRECIP_ERA5,
}
_VAR_ALIASES = {
    "tp": _OUTPUT_PRECIP,
    "precip": _OUTPUT_PRECIP,
    "precipitation": _OUTPUT_PRECIP,
    "total_precipitation": _OUTPUT_PRECIP,
    _NATIVE_PRECIP_0P25: _OUTPUT_PRECIP,
    _NATIVE_PRECIP_1DEG: _OUTPUT_PRECIP,
    _NATIVE_PRECIP_ERA5: _OUTPUT_PRECIP,
    _OUTPUT_PRECIP: _OUTPUT_PRECIP,
}

# YYYY-MM-DD-HH.zarr  (init date, init hour) — one store per init, all leads
_STORE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2})\.zarr$")
# Lead-time dim names the store may use instead of ``step``.
_LEAD_DIMS = ("step", "lead_time", "prediction_timedelta", "lead")
# Init-time dim names (length 1 per store) dropped in favour of a scalar ``time``.
_INIT_DIMS = ("init_time", "forecast_reference_time", "time")
_MEMBER_DIMS = ("ensemble_member", "member", "realization")


def _blob_root() -> str:
    return f"{_CONTAINER}/{_RUN_PREFIX}/{_ZARR_DIR}"


def _data_prefix(dataset: str) -> str:
    return f"{_blob_root()}/{dataset}"


def _display_prefix(dataset: str | None = None) -> str:
    path = _blob_root() if dataset is None else _data_prefix(dataset)
    return f"az://{_ACCOUNT}/{path}/"


def _normalize_sas(token: str) -> str:
    """Accept a raw SAS query, an optional leading ``?``, or a full blob URL."""
    text = token.strip().strip('"').strip("'")
    if "://" in text:
        text = text.split("?", 1)[-1]
    return text.removeprefix("?")


def _redact(text: str) -> str:
    return _SIG_RE.sub(r"\1REDACTED", text)


def _sas_token() -> str:
    (raw,) = require_env(
        _SAS_ENV,
        message=(
            f"{_SAS_ENV} must be set to a read SAS for Azure container "
            f"{_ACCOUNT}/{_CONTAINER} (list and read: sp=rl)."
        ),
    )
    token = _normalize_sas(raw)
    if "sig=" not in token.lower():
        raise UsageError(
            f"{_SAS_ENV} is set but does not look like an Azure SAS "
            "(expected a query string containing sig=)."
        )
    return token


def _storage_options() -> dict[str, str]:
    return {"account_name": _ACCOUNT, "sas_token": _sas_token()}


def _canonical_dataset(dataset: str) -> str:
    key = (dataset or _DEFAULT_DATASET).strip()
    return _DATASET_ALIASES.get(key, key)


def _parse_name(name: str) -> tuple[str, str] | None:
    match = _STORE_RE.fullmatch(name)
    if not match:
        return None
    return match.group(1), match.group(2)


def _filesystem():
    options = _storage_options()
    import adlfs

    try:
        return adlfs.AzureBlobFileSystem(**options)
    except UsageError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface SAS failures as DataError
        raise DataError(
            f"could not open Azure Blob Storage account {_ACCOUNT} "
            f"({_redact(f'{type(exc).__name__}: {exc}')}). "
            f"Set {_SAS_ENV} to a read SAS for container {_CONTAINER}."
        ) from None


def _azure_error(exc: Exception, what: str) -> DataError:
    text = _redact(f"{type(exc).__name__}: {exc}")
    hint = ""
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "401",
            "403",
            "forbidden",
            "authentication",
            "authorization",
            "signature",
        )
    ):
        hint = f" Check {_SAS_ENV}: it needs list and read (sp=rl) on {_ACCOUNT}/{_CONTAINER}."
    return DataError(f"{what} ({text}).{hint}")


def _list_names(prefix: str) -> list[str]:
    fs = _filesystem()
    try:
        items = fs.ls(prefix, detail=False)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(exc, f"Azure listing failed for az://{_ACCOUNT}/{prefix}/") from None
    names = []
    for item in items:
        name = str(item).rstrip("/").rsplit("/", 1)[-1]
        if name and name not in {".", ".."} and not name.endswith("/"):
            names.append(name)
    return names


def _list_objects(dataset: str) -> list[str]:
    """Basenames (``YYYY-MM-DD-HH.zarr`` stores) under the variable prefix."""
    return _list_names(_data_prefix(dataset))


def _list_datasets() -> list[str]:
    return sorted(_list_names(_blob_root()))


def _init_dates_from_names(names: list[str]) -> list[str]:
    dates = {parsed[0] for name in names if (parsed := _parse_name(name))}
    return sorted(dates)


def _list_init_dates(dataset: str) -> list[str]:
    return _init_dates_from_names(_list_objects(dataset))


def _resolve_date(date, dataset: str) -> str:
    dates = _list_init_dates(dataset)
    if date is not None:
        iso = date.isoformat()
        if iso not in dates:
            available = f"{dates[0]} .. {dates[-1]}" if dates else "none"
            raise DataError(
                f"no {dataset!r} Cumulus init {iso} in {_display_prefix(dataset)} "
                f"(available init dates: {available}). "
                "Omit --date to take the latest, or pass --probe-latest."
            )
        return iso
    if not dates:
        available = _list_datasets()
        hint = f" Available --dataset folders: {', '.join(available)}." if available else ""
        raise DataError(
            f"no YYYY-MM-DD-HH.zarr stores under {_display_prefix(dataset)}.{hint}".rstrip()
        )
    return dates[-1]


def _store_url(dataset: str, iso: str) -> str:
    """URL of the init's Zarr store; the latest init hour wins if several exist."""
    hours = sorted(
        parsed[1]
        for name in _list_objects(dataset)
        if (parsed := _parse_name(name)) and parsed[0] == iso
    )
    if not hours:
        raise DataError(f"no Zarr store for init {iso} under {_display_prefix(dataset)}")
    return f"az://{_data_prefix(dataset)}/{iso}-{hours[-1]}.zarr"


def _normalize_lead_axis(ds, iso: str):
    """Put the lead axis on a timedelta ``step`` dim, members on ``number``, and drop init dims."""
    import numpy as np

    for name in _LEAD_DIMS[1:]:
        if name in ds.dims and "step" not in ds.dims:
            ds = ds.rename({name: "step"})
    for name in _MEMBER_DIMS:
        if name in ds.dims and "number" not in ds.dims:
            ds = ds.rename({name: "number"})
    if "time" in ds.dims and "step" not in ds.dims and ds.sizes["time"] > 1:
        # Valid-time axis: express it as lead from the init.
        init = np.datetime64(iso, "ns")
        ds = ds.assign_coords(step=("time", ds["time"].values - init))
        ds = ds.swap_dims({"time": "step"})
    for name in _INIT_DIMS:
        if name in ds.dims:
            ds = ds.isel({name: 0})
    ds = ds.drop_vars([c for c in (*_INIT_DIMS, "valid_time") if c in ds.coords])
    if "step" in ds.coords and not np.issubdtype(ds["step"].dtype, np.timedelta64):
        units = str(ds["step"].attrs.get("units", "hours")).split()[0].lower()
        unit = {"days": "D", "day": "D", "hours": "h", "hour": "h"}.get(units, "h")
        ds = ds.assign_coords(
            step=ds["step"].values.astype(f"timedelta64[{unit}]").astype("timedelta64[ns]")
        )
    return ds


def _open_init(dataset: str, iso: str, bbox, workers: int):
    import xarray as xr
    import zarr

    url = _store_url(dataset, iso)
    print(f"Opening Cumulus store {url.rsplit('/', 1)[-1]} for {iso}", file=sys.stderr)
    try:
        with zarr.config.set({"async.concurrency": max(1, int(workers))}):
            with xr.open_zarr(
                url, storage_options=_storage_options(), chunks=None, decode_timedelta=True
            ) as opened:
                ds = bbox_subset(opened, bbox) if bbox is not None else opened
                ds = ds.load()
    except (DataError, UsageError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(exc, f"failed to open {url}") from None
    ds = _normalize_lead_axis(ds, iso)
    return ds.sortby("step") if "step" in ds.dims else ds


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


def _prepare_dataset(ds, iso: str):
    import numpy as np

    for native in _NATIVE_PRECIPS:
        if native in ds.data_vars:
            ds = ds.rename({native: _OUTPUT_PRECIP})
            break
    if _OUTPUT_PRECIP in ds.data_vars:
        ds[_OUTPUT_PRECIP] = ds[_OUTPUT_PRECIP].clip(min=0)
        ds[_OUTPUT_PRECIP].attrs.setdefault("units", "mm")
        ds[_OUTPUT_PRECIP].attrs.setdefault("long_name", "Total precipitation")

    order = [d for d in ("number", "step", "latitude", "longitude") if d in ds.dims]
    others = [d for d in ds.dims if d not in order]
    if order:
        ds = ds.transpose(*others, *order)

    ds = ds.assign_coords(time=np.datetime64(iso, "ns"))
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
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
            f"variable(s) not in this Cumulus product: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(ds.data_vars))}"
        )
    return ds[resolved]


@weather_skill(
    name="cumulus-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--dataset",
    default=_DEFAULT_DATASET,
    help=(f"Variable folder under zarr/ (default precip → {_NATIVE_PRECIP})."),
)
@weather_skill.argument("--date")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"Max concurrent Zarr chunk requests (default {DEFAULT_WORKERS}).",
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
        "Does not download fields. Optional IDENT selects a --dataset id."
    ),
)
def fetch(dataset, date, bbox, variable, output, **kwargs):
    """Fetch a Cumulus AI operational ensemble forecast and write a weather-skills standard dataset.

    Opens the init's Zarr store
    ``az://italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/<variable>/YYYY-MM-DD-HH.zarr``
    with the SAS in ``AZURE_STORAGE_SAS_TOKEN``, optionally subsets by
    ``--bbox`` / ``--variable``, and returns a Dataset for the decorator to write.
    """
    dsid = _canonical_dataset(kwargs["probe_latest"] or dataset)
    if kwargs.get("probe_latest") is not None:
        dates = _list_init_dates(dsid)
        print(dates[-1] if dates else "none")
        return

    dataset = _canonical_dataset(dataset)
    iso = _resolve_date(date, dataset)
    print(f"Resolved init date: {iso}", file=sys.stderr)
    print(
        f"Opening {_display_prefix(dataset)}",
        file=sys.stderr,
    )

    workers = kwargs.get("workers", DEFAULT_WORKERS)
    ds = _open_init(dataset, iso, bbox, workers)
    ds = _prepare_dataset(ds, iso)
    ds = _select_variables(ds, variable)
    if bbox is None:
        ds = ensure_normalized_longitude(ds)

    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"cumulus:{dataset}",
    )
    stamp_cf_attrs(ds)
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval="1 day", deaccumulate=False)
    ds = _shift_step_origin_to_zero(ds)
    ds = stamp_data_interval(ds)
    return ds


if __name__ == "__main__":
    fetch()

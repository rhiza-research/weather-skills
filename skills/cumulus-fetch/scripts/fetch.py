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
_SAS_ENV = "AZURE_STORAGE_SAS_TOKEN"
# One Zarr store per init: <_ROOT>/<folder>/YYYY-MM-DD-HH.zarr
_ROOT = f"{_CONTAINER}/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr"
_STORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}\.zarr$")

# --dataset name → variable folder. Each folder's stores hold that one variable.
_DATASETS = {
    "precip": "total_precipitation_24h_acc_imerg_0p25",  # IMERG-trained, 0.25° (default)
    "precip_1deg": "total_precipitation_24h_acc_imerg",  # IMERG-trained, native 1°
    "precip_era5": "total_precipitation_24h_acc_era5",  # ERA5-trained
}
# Accepted -v names; the output variable is always ``tp``.
_TP_ALIASES = {"tp", "precip", "precipitation", "total_precipitation", *_DATASETS.values()}
DEFAULT_WORKERS = 8


def _storage_options() -> dict[str, str]:
    """adlfs options from the SAS in the env (raw query, leading ``?``, or full blob URL)."""
    (raw,) = require_env(
        _SAS_ENV,
        message=(
            f"{_SAS_ENV} must be set to a read SAS for Azure container "
            f"{_ACCOUNT}/{_CONTAINER} (list and read: sp=rl)."
        ),
    )
    token = raw.strip().strip("'\"").split("?", 1)[-1]
    if "sig=" not in token.lower():
        raise UsageError(
            f"{_SAS_ENV} is set but does not look like an Azure SAS "
            "(expected a query string containing sig=)."
        )
    return {"account_name": _ACCOUNT, "sas_token": token}


def _azure_error(exc: Exception, what: str) -> DataError:
    """Wrap an Azure failure as a DataError, with the SAS signature redacted."""
    text = re.sub(r"(sig=)[^&\s'\"<>]+", r"\1REDACTED", f"{type(exc).__name__}: {exc}", flags=re.I)
    hint = ""
    if any(s in text.lower() for s in ("401", "403", "forbidden", "authenticat", "authoriz")):
        hint = f" Check {_SAS_ENV}: it needs list and read (sp=rl) on {_ACCOUNT}/{_CONTAINER}."
    return DataError(f"{what} ({text}).{hint}")


def _list_stores(folder: str) -> list[str]:
    """``YYYY-MM-DD-HH.zarr`` store names in a variable folder, oldest first."""
    options = _storage_options()  # check the SAS before importing adlfs
    import adlfs

    fs = adlfs.AzureBlobFileSystem(**options)
    try:
        paths = fs.ls(f"{_ROOT}/{folder}", detail=False)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(
            exc, f"Azure listing failed for az://{_ACCOUNT}/{_ROOT}/{folder}/"
        ) from None
    names = (path.rstrip("/").rsplit("/", 1)[-1] for path in paths)
    return sorted(name for name in names if _STORE_RE.fullmatch(name))


def _open_store(url: str, bbox, workers: int):
    """Open one init's store and load it, subset to ``bbox``."""
    import xarray as xr
    import zarr

    try:
        with (
            zarr.config.set({"async.concurrency": workers}),
            xr.open_zarr(url, storage_options=_storage_options(), chunks=None) as ds,
        ):
            return (bbox_subset(ds, bbox) if bbox is not None else ds).load()
    except (DataError, UsageError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(exc, f"failed to open {url}") from None


def _to_standard(ds):
    """Map a store's (init_time, lead_time, ensemble_member, lat, lon) onto the standard layout."""
    import numpy as np

    (name,) = ds.data_vars
    ds = ds.squeeze("init_time").drop_vars("valid_time", errors="ignore")
    ds = ds.rename(
        {name: "tp", "init_time": "time", "lead_time": "step", "ensemble_member": "number"}
    )
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    # Each lead is the 24h total ending at init + lead; label it by the period start.
    ds = ds.assign_coords(step=ds["step"] - np.timedelta64(24, "h"))
    return ds.transpose("number", "step", "latitude", "longitude")


@weather_skill(
    name="cumulus-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--dataset",
    default="precip",
    help=f"One of {', '.join(_DATASETS)} (default precip), or a variable folder name.",
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
    ``--bbox``, and returns a Dataset for the decorator to write.
    """
    probe = kwargs.get("probe_latest")
    name = probe or dataset
    folder = _DATASETS.get(name, name)
    prefix = f"az://{_ACCOUNT}/{_ROOT}/{folder}/"

    if probe is not None:
        stores = _list_stores(folder)
        print(stores[-1][:10] if stores else "none")
        return

    unknown = set(variable or ()) - _TP_ALIASES
    if unknown:
        raise UsageError(
            f"variable(s) not in this Cumulus product: {', '.join(sorted(unknown))}.\nAvailable: tp"
        )

    # Names sort by init date then hour, so the last match is the latest init.
    stores = _list_stores(folder)
    if date is not None:
        stores = [s for s in stores if s.startswith(date.isoformat())]
    if not stores:
        wanted = f"init {date.isoformat()}" if date else "init stores"
        raise DataError(
            f"no Cumulus {wanted} under {prefix}. "
            "Omit --date to take the latest, or pass --probe-latest to see what is published."
        )
    store = stores[-1]
    print(f"Opening {prefix}{store}", file=sys.stderr)

    ds = _open_store(f"az://{_ROOT}/{folder}/{store}", bbox, kwargs["workers"])
    ds = _to_standard(ds)
    if bbox is None:
        ds = ensure_normalized_longitude(ds)

    ds.attrs.update(Conventions="CF-1.13", weather_skills_source=f"cumulus:{folder}")
    stamp_cf_attrs(ds)
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval="1 day", deaccumulate=False)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "adlfs",
#   "h5netcdf",
#   "h5py",
# ]
# ///
"""Fetch the Cumulus AI ensemble precipitation forecast for one init date as a weather-skills Zarr."""

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
_SKILL_VERSION = "0.0.2"

_ACCOUNT = "italynorthdata"
_CONTAINER = "data"
_SAS_ENV = "AZURE_STORAGE_SAS_TOKEN"
# One Zarr store per init: <_ROOT>/<folder>/YYYY-MM-DD-HH.zarr
_ROOT = f"{_CONTAINER}/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr"
_STORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}\.zarr$")
# Hindcasts: one NetCDF per init and lead, control and perturbed members apart:
# <_HINDCAST_ROOT>/{cf,pf}/<folder>/data/YYYY-MM-DD-HH-LLLL.nc (LLLL = lead hours)
_HINDCAST_RUN = "gratis-czajkowski"
_HINDCAST_ROOT = f"{_CONTAINER}/ml_reforecasts/{_HINDCAST_RUN}"
_HINDCAST_LEADS = [f"{24 * (i + 1):04d}" for i in range(46)]
_HINDCAST_FIRST_YEAR = 2016

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
    # Also the URL-encoded form (sig%3D...) some clients echo in errors.
    text = re.sub(
        r"(sig(?:=|%3D))[^&\s'\"<>%]+(?:%[0-9A-F]{2}[^&\s'\"<>%]*)*",
        r"\1REDACTED",
        f"{type(exc).__name__}: {exc}",
        flags=re.I,
    )
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


def _list_hindcast_inits(folder: str, prefix: str) -> list[str]:
    """``YYYY-MM-DD-HH`` hindcast inits whose name starts with ``prefix``, oldest first."""
    options = _storage_options()
    import adlfs

    fs = adlfs.AzureBlobFileSystem(**options)
    data = f"{_HINDCAST_ROOT}/pf/{folder}/data"
    try:
        paths = fs.find(data, prefix=prefix)
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(exc, f"Azure listing failed for az://{_ACCOUNT}/{data}/") from None
    return sorted({path.rsplit("/", 1)[-1][:13] for path in paths if path.endswith(".nc")})


def _open_hindcast(folder: str, init: str, bbox, workers: int):
    """Read one hindcast init's per-lead NetCDF files into the live store layout.

    The live Zarr export puts the ``cf`` file as member 0 ahead of the 29
    ``pf`` members (1-29) and sets negative model output to 0; this
    does the same so a hindcast init matches a live store.
    """
    import io
    from concurrent.futures import ThreadPoolExecutor

    import adlfs
    import numpy as np
    import xarray as xr

    fs = adlfs.AzureBlobFileSystem(**_storage_options())

    def read(path):
        try:
            raw = fs.cat_file(path)
        except FileNotFoundError:
            raise DataError(
                f"Cumulus hindcast init {init} is missing az://{_ACCOUNT}/{path}; "
                f"every init should have all {len(_HINDCAST_LEADS)} leads. Leave it out of "
                "--start-time..--end-time."
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise _azure_error(exc, f"failed to read az://{_ACCOUNT}/{path}") from None
        with xr.open_dataset(io.BytesIO(raw), engine="h5netcdf") as ds:
            return (bbox_subset(ds, bbox) if bbox is not None else ds)[folder].load()

    paths = [
        f"{_HINDCAST_ROOT}/{ens}/{folder}/data/{init}-{lead}.nc"
        for lead in _HINDCAST_LEADS
        for ens in ("cf", "pf")
    ]
    with ThreadPoolExecutor(workers) as pool:
        fields = list(pool.map(read, paths))
    # fields alternate cf, pf per lead; cf has no member dim.
    leads = [
        xr.concat([cf.expand_dims(number=[0]), pf], "number")
        for cf, pf in zip(fields[::2], fields[1::2], strict=True)
    ]
    da = xr.concat(leads, "lead_time").clip(min=0)
    da = da.rename(number="ensemble_member").expand_dims(
        init_time=[np.datetime64(f"{init[:10]}T{init[11:]}", "ns")]
    )
    da = da.assign_coords(lead_time=[np.timedelta64(int(lead), "h") for lead in _HINDCAST_LEADS])
    da.attrs.update(
        units="mm",
        long_name="24-hour total precipitation",
        cell_methods="time: sum (interval: 24 hours)",
    )
    return da.transpose(
        "init_time", "lead_time", "ensemble_member", "latitude", "longitude"
    ).to_dataset(name=folder)


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
    """Map a store's (init_time, lead_time, ensemble_member, lat, lon) onto the standard layout.

    A one-init live store passes init_time squeezed to a scalar; hindcasts keep it as a dim.
    """
    import numpy as np

    (name,) = ds.data_vars
    ds = ds.drop_vars("valid_time", errors="ignore")
    ds = ds.rename(
        {name: "tp", "init_time": "time", "lead_time": "step", "ensemble_member": "number"}
    )
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    # Each lead is the 24h total ending at init + lead; label it by the period start.
    ds = ds.assign_coords(step=ds["step"] - np.timedelta64(24, "h"))
    ds["tp"].attrs["comment"] = (
        "24 h precipitation from init + step to init + step + 24 h "
        "(cumulus-fetch relabelled store leads from period end to period start)."
    )
    return ds.transpose(..., "number", "step", "latitude", "longitude")


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
@weather_skill.argument(
    "--hindcast",
    action="store_true",
    help=f"Read the {_HINDCAST_RUN} hindcasts (inits every second day, 2016-2024) "
    "with init dates in --start-time..--end-time, instead of one live forecast.",
)
@weather_skill.argument("--start-time", help="--hindcast: first init date (inclusive).")
@weather_skill.argument("--end-time", help="--hindcast: last init date (inclusive).")
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
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a --dataset id."
    ),
)
def fetch(
    dataset, date, hindcast, start_time, end_time, bbox, variable, probe_latest, workers, **kwargs
):
    """Fetch the Cumulus AI ensemble precipitation forecast for one init date as a weather-skills Zarr."""
    # Opens the init's Zarr store
    # ``az://italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/<variable>/YYYY-MM-DD-HH.zarr``
    # (or, with --hindcast, each init's per-lead NetCDF files under
    # ``az://italynorthdata/data/ml_reforecasts/gratis-czajkowski``, stacked on time)
    # with the SAS in ``AZURE_STORAGE_SAS_TOKEN``, optionally subsets by
    # ``--bbox``, and returns a Dataset for the decorator to write.
    import datetime as dt

    import numpy as np
    import xarray as xr

    probe = probe_latest
    name = probe or dataset
    folder = _DATASETS.get(name, name)
    if folder not in _DATASETS.values():
        raise UsageError(
            f"unknown Cumulus dataset {name!r}. Available: {', '.join(_DATASETS)} "
            f"(or a folder name: {', '.join(_DATASETS.values())})."
        )
    if workers < 1:
        raise UsageError(f"--workers must be at least 1 (got {workers}).")
    unknown = set(variable or ()) - _TP_ALIASES
    if probe is None and unknown:
        raise UsageError(
            f"variable(s) not in this Cumulus product: {', '.join(sorted(unknown))}.\nAvailable: tp"
        )
    if hindcast and probe is None:
        if date is not None:
            raise UsageError("--hindcast takes --start-time and --end-time, not --date.")
        if start_time is None or end_time is None:
            raise UsageError("--hindcast needs --start-time and --end-time (inclusive init dates).")
        if start_time > end_time:
            raise UsageError(f"--start-time {start_time} is after --end-time {end_time}.")
    elif not hindcast and (start_time is not None or end_time is not None):
        raise UsageError(
            "--start-time and --end-time apply to --hindcast only; live forecasts take --date."
        )

    if hindcast:
        # A whole hindcast folder is ~45,000 files: list one year at a time.
        prefix = f"az://{_ACCOUNT}/{_HINDCAST_ROOT}/{{cf,pf}}/{folder}/data/"
        if probe is not None:
            for year in range(dt.date.today().year, _HINDCAST_FIRST_YEAR - 1, -1):
                inits = _list_hindcast_inits(folder, f"{year}-")
                if inits:
                    break
            print(inits[-1][:10] if inits else "none")
            return
        inits = [
            init
            for year in range(start_time.year, end_time.year + 1)
            for init in _list_hindcast_inits(folder, f"{year}-")
            if start_time.isoformat() <= init[:10] <= end_time.isoformat()
        ]
        if not inits:
            raise DataError(
                f"no Cumulus hindcast init from {start_time} to {end_time} under {prefix}. "
                "Hindcast inits are every second day at 00 UTC, 2016-2024; "
                "pass --probe-latest --hindcast for the latest."
            )
        # Each init is ~5.7 GB of global files, read whole and cut to the bbox: one at a time.
        pieces = []
        for n, init in enumerate(inits, 1):
            print(f"Reading ({n}/{len(inits)}) {prefix}{init}-LLLL.nc", file=sys.stderr)
            pieces.append(_open_hindcast(folder, init, bbox, workers))
        ds = _to_standard(xr.concat(pieces, "init_time"))
        folder = f"{_HINDCAST_RUN}/{folder}"
    else:
        # Names sort by init date then hour, so the last match is the latest init.
        prefix = f"az://{_ACCOUNT}/{_ROOT}/{folder}/"
        stores = _list_stores(folder)
        if probe is not None:
            print(stores[-1][:10] if stores else "none")
            return

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
        ds = _open_store(f"az://{_ROOT}/{folder}/{store}", bbox, workers)
        ds = _to_standard(ds.squeeze("init_time"))
        # A fresh init may still be writing its long leads (all-NaN). Error when
        # nothing is there; drop an empty tail and say so instead of writing it.
        empty = ds["tp"].isnull().all(("number", "latitude", "longitude")).values
        if empty.all():
            raise DataError(
                f"Cumulus init {store} has no data in the requested bbox (still publishing?). "
                "Pass --date with an earlier init."
            )
        last = int(np.nonzero(~empty)[0][-1])
        if last < ds.sizes["step"] - 1:
            days = ds["step"].values / np.timedelta64(1, "D")
            print(
                f"WARNING: Cumulus init {store} has no data from step {days[last + 1]:g} d "
                f"(of {days[-1]:g} d; still publishing?). Dropped the empty leads (not padded); "
                "pass --date with an earlier init for every lead.",
                file=sys.stderr,
            )
            ds = ds.isel(step=slice(0, last + 1))
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

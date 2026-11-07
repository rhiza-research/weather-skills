# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
#   "adlfs",
# ]
# ///
"""Fetch the Cumulus AI ensemble precipitation forecast for one init date or a range of inits as a weather-skills Zarr."""

from __future__ import annotations

import bisect
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
_SKILL_VERSION = "0.0.9"

_ACCOUNT = "italynorthdata"
_CONTAINER = "data"
_SAS_ENV = "AZURE_STORAGE_SAS_TOKEN"
# One Zarr store per init: <_ROOT>/<folder>/YYYY-MM-DD-HH.zarr
_ROOT = f"{_CONTAINER}/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr"
_STORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}\.zarr$")
# Inits before the first live store come from Cumulus's own Aurora hindcast: one Zarr
# of every init, same layout as a live store, IMERG-trained precip on the native 1° grid.
_HINDCAST_RUN = "gratis-czajkowski-era5"
_HINDCAST = (
    f"{_CONTAINER}/ml_reforecasts/{_HINDCAST_RUN}/zarr/total_precipitation_24h_acc_imerg.zarr"
)

# --dataset name → variable folder. Each folder's stores hold that one variable.
_DATASETS = {
    "precip": "total_precipitation_24h_acc_imerg_0p25",  # IMERG-trained, 0.25° (default)
    "precip_1deg": "total_precipitation_24h_acc_imerg",  # IMERG-trained, native 1°
    "precip_era5": "total_precipitation_24h_acc_era5",  # ERA5-trained
}
# Accepted -v names; the output variable is always ``precip``.
_PRECIP_ALIASES = {"tp", "precip", "precipitation", "total_precipitation", *_DATASETS.values()}
DEFAULT_WORKERS = 8


def _storage_options() -> dict[str, str]:
    """adlfs options from the SAS in the env (raw query, leading ``?``, or full blob URL)."""
    (raw,) = require_env(
        _SAS_ENV,
        message=(
            f"{_SAS_ENV} is not set in this workspace, so Cumulus cannot be read; retrying "
            "will not help. Ask the workspace admin to add the Cumulus credential: a read "
            f"SAS for Azure container {_ACCOUNT}/{_CONTAINER} (list and read: sp=rl)."
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


def _open_store(url: str, bbox, workers: int, select=None):
    """Open a store and load it, after ``select`` (lazy dataset -> dataset) and the ``bbox`` cut."""
    import weakref

    import xarray as xr
    import zarr
    from adlfs.utils import close_service_client
    from zarr.core.sync import sync

    try:
        with zarr.config.set({"async.concurrency": workers}):
            # A fresh filesystem per store: the session is closed below, and
            # fsspec's instance cache would hand that closed one to the next init.
            store = zarr.storage.FsspecStore.from_url(
                url,
                storage_options={**_storage_options(), "skip_instance_cache": True},
                read_only=True,
            )
            with xr.open_zarr(store, chunks=None) as ds:
                ds = select(ds) if select is not None else ds
                ds = (bbox_subset(ds, bbox) if bbox is not None else ds).load()
    except (DataError, UsageError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(exc, f"failed to open {url}") from None
    # zarr opens adlfs asynchronously, which leaves the filesystem without an
    # event loop, so adlfs's exit finalizer (sync(None, close_service_client, fs))
    # raises "Loop is not running". Close the Azure session on zarr's loop
    # instead and drop that finalizer.
    sync(close_service_client(store.fs))
    for finalizer in list(weakref.finalize._registry):
        if (finalizer.peek() or (None,))[0] is store.fs:
            finalizer.detach()
    return ds


def _to_standard(ds):
    """Map a store's (init_time, lead_time, ensemble_member, lat, lon) onto the standard layout."""
    import numpy as np

    (name,) = ds.data_vars
    ds = ds.squeeze("init_time").drop_vars("valid_time", errors="ignore")
    ds = ds.rename(
        {name: "precip", "init_time": "time", "lead_time": "step", "ensemble_member": "number"}
    )
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    # Each lead is the 24h total ending at init + lead; label it by the period start.
    ds = ds.assign_coords(step=ds["step"] - np.timedelta64(24, "h"))
    ds["precip"].attrs["comment"] = (
        "24 h precipitation from init + step to init + step + 24 h "
        "(cumulus-fetch relabelled store leads from period end to period start)."
    )
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
@weather_skill.argument(
    "--start-time",
    action="append",
    help="Init window start; repeat with --end-time for several windows (each January).",
)
@weather_skill.argument(
    "--end-time", action="append", help="Init window end, paired with --start-time."
)
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
def fetch(dataset, date, start_time, end_time, bbox, variable, probe_latest, workers, **kwargs):
    """Fetch the Cumulus AI ensemble precipitation forecast for one init date or a range of inits as a weather-skills Zarr."""
    # Opens the init's Zarr store
    # ``az://italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/<variable>/YYYY-MM-DD-HH.zarr``
    # with the SAS in ``AZURE_STORAGE_SAS_TOKEN``, optionally subsets by
    # ``--bbox``, and returns a Dataset for the decorator to write.
    import numpy as np

    probe = probe_latest
    name = probe or dataset
    folder = _DATASETS.get(name, name)
    if folder not in _DATASETS.values():
        raise UsageError(
            f"unknown Cumulus dataset {name!r}. Available: {', '.join(_DATASETS)} "
            f"(or a folder name: {', '.join(_DATASETS.values())}). Inits before the live "
            "record are read from the Cumulus hindcast automatically: just pass the date."
        )
    if workers < 1:
        raise UsageError(f"--workers must be at least 1 (got {workers}).")
    prefix = f"az://{_ACCOUNT}/{_ROOT}/{folder}/"

    if probe is not None:
        stores = _list_stores(folder)
        print(stores[-1][:10] if stores else "none")
        return

    unknown = set(variable or ()) - _PRECIP_ALIASES
    if unknown:
        raise UsageError(
            f"variable(s) not in this Cumulus product: {', '.join(sorted(unknown))}.\nAvailable: precip"
        )

    # One (start, end) pair per window; --end-time is an inclusive calendar day.
    windows = list(zip(start_time or [], end_time or [], strict=True))
    if date is not None and windows:
        raise UsageError(
            "pass --date for one init, or --start-time/--end-time for a range of inits (not both)."
        )

    # Names sort by init date then hour, so the last match is the latest init.
    stores = _list_stores(folder)
    # Inits before the first live store are read from the hindcast; one call reads one source.
    first_live = stores[0][:10] if stores else "9999-12-31"
    hindcast = date is not None and date.isoformat() < first_live
    if windows:
        hindcast = any(a.isoformat() < first_live for a, _ in windows)
    if hindcast:
        # Just the coordinates: list the hindcast inits under the live store names.
        inits = _open_store(
            f"az://{_HINDCAST}", None, workers, lambda ds: ds.drop_vars(list(ds.data_vars))
        )["init_time"].values
        hindcast_stores = [f"{str(t)[:10]}-{str(t)[11:13]}.zarr" for t in inits]
        if windows and any(b.isoformat() >= first_live for _, b in windows):
            if any(
                a.isoformat() <= s[:10] <= b.isoformat()
                for s in hindcast_stores
                for a, b in windows
            ):
                raise UsageError(
                    f"the init windows cross {first_live}, where the live Cumulus runs start. "
                    f"Earlier inits come from the Cumulus hindcast ({_HINDCAST_RUN}, 1° grid, "
                    "a different store), so one call reads only one side: "
                    f"split the request at {first_live} into two calls."
                )
            hindcast = False  # the windows hold no hindcast init: read the live side only
    if hindcast:
        if folder == _DATASETS["precip_era5"]:
            raise UsageError(
                f"no ERA5-trained Cumulus hindcast: inits before {first_live} exist only for "
                "precip_1deg (IMERG-trained, 1°). Use --dataset precip_1deg, or inits from "
                f"{first_live}."
            )
        if folder == _DATASETS["precip"]:
            print(
                f"WARNING: inits before {first_live} come from the Cumulus hindcast, which is "
                "on a 1° grid (not 0.25°); writing 1°.",
                file=sys.stderr,
            )
        prefix = f"az://{_ACCOUNT}/{_HINDCAST}"
        stores = hindcast_stores
    published = stores
    if windows:
        # 00 UTC inits only: the windows select init times, not valid times.
        stores = [
            s
            for s in published
            if s.endswith("-00.zarr")
            and any(a.isoformat() <= s[:10] <= b.isoformat() for a, b in windows)
        ]
        if not stores:
            window_text = ", ".join(f"{a}..{b}" for a, b in windows)
            raise DataError(
                f"no 00 UTC Cumulus init in {window_text} under {prefix}; "
                + (
                    f"published inits run {published[0][:10]}..{published[-1][:10]}."
                    if published
                    else "nothing is published."
                )
            )
        print(f"Selected {len(stores)} inits.", file=sys.stderr)
    elif date is not None:
        stores = [s for s in stores if s.startswith(date.isoformat())]
    if not stores:
        if date is None:
            raise DataError(f"no Cumulus init stores under {prefix}.")
        # List the published inits around the requested date so one call is enough.
        dates = sorted({s[:10] for s in published})
        i = bisect.bisect_left(dates, date.isoformat())
        near = dates[max(0, i - 3) : i + 3]
        raise DataError(
            f"no Cumulus init {date.isoformat()} under {prefix}. "
            + (
                f"Nearest published inits: {', '.join(near)} "
                f"({len(dates)} inits from {dates[0]} to {dates[-1]}). "
                if dates
                else "Nothing is published. "
            )
            + "Omit --date to take the latest."
        )
    if not windows:
        stores = stores[-1:]
    # One store per init: each is opened, cut to the bbox and loaded on its own.
    pieces = []
    if hindcast:
        print(
            f"Reading {len(stores)} init(s) from the Cumulus hindcast {_HINDCAST_RUN} "
            f"(IMERG-trained, 1°) at az://{_ACCOUNT}/{_HINDCAST}",
            file=sys.stderr,
        )
        times = [np.datetime64(f"{s[:10]}T{s[11:13]}", "ns") for s in stores]
        ds = _open_store(f"az://{_HINDCAST}", bbox, workers, lambda ds: ds.sel(init_time=times))
        pieces = [_to_standard(ds.isel(init_time=[i])) for i in range(len(times))]
    for store in [] if hindcast else stores:
        print(f"Opening {prefix}{store}", file=sys.stderr)
        pieces.append(_to_standard(_open_store(f"az://{_ROOT}/{folder}/{store}", bbox, workers)))
    if windows:
        import xarray as xr

        # A range of inits: a `time` axis (the init). An init still writing its
        # long leads keeps them NaN (not filled); say which init is short.
        ds = xr.concat(pieces, "time", join="outer").transpose("time", "step", ...)
        init_empty = ds["precip"].isnull().all(("number", "latitude", "longitude")).values
        if init_empty.all():
            raise DataError(
                f"no selected Cumulus init has data in the requested bbox under {prefix}."
            )
        days = ds["step"].values / np.timedelta64(1, "D")
        for i, row in enumerate(init_empty):
            init = str(ds["time"].values[i])[:10]
            if row.all():
                print(f"WARNING: Cumulus init {init} has no data (NaN).", file=sys.stderr)
            elif row[-1]:
                print(
                    f"WARNING: Cumulus init {init} has no data from step "
                    f"{days[np.nonzero(~row)[0][-1] + 1]:g} d (still publishing?); its later "
                    "steps are NaN (not filled).",
                    file=sys.stderr,
                )
    else:
        ds = pieces[0]
        # A fresh init may still be writing its long leads (all-NaN). Error when
        # nothing is there; drop an empty tail and say so instead of writing it.
        empty = ds["precip"].isnull().all(("number", "latitude", "longitude")).values
        if empty.all():
            raise DataError(
                f"Cumulus init {stores[-1]} has no data in the requested bbox (still publishing?). "
                "Pass --date with an earlier init."
            )
        last = int(np.nonzero(~empty)[0][-1])
        if last < ds.sizes["step"] - 1:
            days = ds["step"].values / np.timedelta64(1, "D")
            print(
                f"WARNING: Cumulus init {stores[-1]} has no data from step {days[last + 1]:g} d "
                f"(of {days[-1]:g} d; still publishing?). Dropped the empty leads (not padded); "
                "pass --date with an earlier init for every lead.",
                file=sys.stderr,
            )
            ds = ds.isel(step=slice(0, last + 1))
    if bbox is None:
        ds = ensure_normalized_longitude(ds)

    source = f"cumulus-hindcast:{_HINDCAST_RUN}" if hindcast else f"cumulus:{folder}"
    ds.attrs.update(Conventions="CF-1.13", weather_skills_source=source)
    stamp_cf_attrs(ds)
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval="1 day", deaccumulate=False)
    return stamp_data_interval(ds, dim="step" if "step" in ds.dims else None)


if __name__ == "__main__":
    fetch()

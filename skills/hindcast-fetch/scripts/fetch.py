# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
#   "adlfs",
#   "gcsfs",
# ]
# ///
"""Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts for a range of init dates as a weather-skills Zarr."""
# One variable per store, opened as dims (init_time, member, prediction_timedelta, lat, lon)
# in some order:
#   ecmwf — public Sheerwater store. Planette's ECMWF IFS CY49R1 reforecast, inits every
#     second day 2006-2025.
#   ecmwf_wb2 — public Sheerwater store. WeatherBench2's IFS reforecast, one run per init
#     date 1996-2021 from the newest issue that re-ran that date.
#   gefs — public Sheerwater store. CHC's GEFSv12 reforecast, Wednesday inits 1998-2019.
#   cumulus — Cumulus's own Azure Zarr of its gratis-czajkowski-era5 Aurora hindcast,
#     1° IMERG-trained precip and 2 m temperature, 186 inits a year 1996-2024, 30
#     exchangeable members; read with the AZURE_STORAGE_SAS_TOKEN cumulus-fetch uses.

from __future__ import annotations

import re
import sys
import weakref

import numpy as np
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    np_to_date,
    require_env,
)
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_PUBLIC = "gs://sheerwater-public-datalake/hindcasts"
_AZURE_ACCOUNT = "italynorthdata"
_AZURE = "az://data/ml_reforecasts/gratis-czajkowski-era5/zarr"
_SAS_ENV = "AZURE_STORAGE_SAS_TOKEN"
# --dataset -> variable -> (store, variable in the store, units). The stores' own unit
# strings ("C", "mm" for a daily rate) are not all right or pint-readable, so units are set here.
_STORES = {
    "ecmwf": {
        "precip": (f"{_PUBLIC}/ecmwf_planette_precip.zarr", "precip", "mm day-1"),
        "tmp2m": (f"{_PUBLIC}/ecmwf_planette_tmp2m.zarr", "tmp2m", "degree_Celsius"),
        "sst": (f"{_PUBLIC}/ecmwf_planette_sst.zarr", "sst", "degree_Celsius"),
    },
    "ecmwf_wb2": {
        "precip": (f"{_PUBLIC}/ecmwf_ifs_precip.zarr", "precip", "mm day-1"),
        "tmp2m": (f"{_PUBLIC}/ecmwf_ifs_tmp2m.zarr", "tmp2m", "degree_Celsius"),
    },
    "gefs": {
        "precip": (f"{_PUBLIC}/gefs_precip.zarr", "precip", "mm day-1"),
        "tmp2m": (f"{_PUBLIC}/gefs_tmp2m.zarr", "tmp2m", "degree_Celsius"),
    },
    "cumulus": {
        "precip": (
            f"{_AZURE}/total_precipitation_24h_acc_imerg.zarr",
            "total_precipitation_24h_acc_imerg",
            "mm day-1",
        ),
        "tmp2m": (f"{_AZURE}/2m_temperature.zarr", "2m_temperature", "K"),
    },
}
_ALIASES = {"tp": "precip", "precipitation": "precip", "t2m": "tmp2m"}
_STANDARD_NAMES = {
    "precip": "lwe_precipitation_rate",
    "tmp2m": "air_temperature",
    "sst": "sea_surface_temperature",
}
# Azure filesystems opened by this run, closed once their data is loaded.
_AZURE_FS = []


def _sas_options() -> dict[str, str]:
    """adlfs options from the SAS in the env (raw query, leading ``?``, or full blob URL)."""
    (raw,) = require_env(
        _SAS_ENV,
        message=(
            f"{_SAS_ENV} is not set, so the Cumulus hindcast cannot be read. It needs a read "
            f"SAS for Azure container {_AZURE_ACCOUNT}/data (list and read: sp=rl)."
        ),
    )
    token = raw.strip().strip("'\"").split("?", 1)[-1]
    if "sig=" not in token.lower():
        raise UsageError(f"{_SAS_ENV} is set but does not look like an Azure SAS (no sig=).")
    return {"account_name": _AZURE_ACCOUNT, "sas_token": token}


def _redact(exc: Exception) -> str:
    """The error text with any SAS signature (sig=..., sig%3D...) removed."""
    return re.sub(r"(sig(?:=|%3D))[^&\s'\"<>]+", r"\1REDACTED", f"{exc}", flags=re.I)


def _open_store(path: str) -> xr.Dataset:
    """Open a hindcast store lazily as (init_time, member, prediction_timedelta, lat, lon)."""
    if path.startswith(_PUBLIC):
        # Most stores keep prediction_timedelta as integer days with units "days".
        try:
            return xr.open_zarr(path, storage_options={"token": "anon"}, decode_timedelta=True)
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"failed to open {path} ({exc}).") from None

    import zarr

    try:
        store = zarr.storage.FsspecStore.from_url(
            path, storage_options={**_sas_options(), "skip_instance_cache": True}, read_only=True
        )
        ds = xr.open_zarr(store, chunks=None)
    except (DataError, UsageError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed to open {path} ({_redact(exc)}). Check {_SAS_ENV}.") from None
    _AZURE_FS.append(store.fs)
    ds = ds.drop_vars("valid_time", errors="ignore").rename(
        ensemble_member="member", lead_time="prediction_timedelta", latitude="lat", longitude="lon"
    )
    # lead_time labels the 24 h window by its end; the other stores label it by its start.
    return ds.assign_coords(
        prediction_timedelta=ds["prediction_timedelta"] - np.timedelta64(1, "D")
    )


def _close_azure() -> None:
    """Close the Azure sessions on zarr's event loop and drop adlfs's exit finalizer,
    which would otherwise fail with "Loop is not running"."""
    if not _AZURE_FS:
        return
    from adlfs.utils import close_service_client
    from zarr.core.sync import sync

    for fs in _AZURE_FS:
        sync(close_service_client(fs))
        for finalizer in list(weakref.finalize._registry):
            if (finalizer.peek() or (None,))[0] is fs:
                finalizer.detach()
    _AZURE_FS.clear()


@weather_skill(name="hindcast-fetch", version=_SKILL_VERSION)
@weather_skill.argument(
    "--dataset", choices=list(_STORES), help=f"Hindcast source: {', '.join(_STORES)}."
)
@weather_skill.argument(
    "--start-time",
    action="append",
    help="Init window start; repeat with --end-time for several windows (each January).",
)
@weather_skill.argument(
    "--end-time", action="append", help="Init window end, paired with --start-time."
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--members",
    action="store_true",
    help="ecmwf and ecmwf_wb2 only: the 10 perturbed members instead of their mean.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="ecmwf",
    metavar="DATASET",
    probe=True,
    help="Print the latest hindcast init date (YYYY-MM-DD) for DATASET (default ecmwf) and exit.",
)
def fetch(dataset, start_time, end_time, variable, bbox, members, probe_latest, **kwargs):
    """Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts for a range of init dates as a weather-skills Zarr."""
    if probe_latest is not None:
        if probe_latest not in _STORES:
            raise UsageError(
                f"--probe-latest takes one of {', '.join(_STORES)}; got {probe_latest!r}."
            )
        inits = _open_store(_STORES[probe_latest]["precip"][0])["init_time"].values
        _close_azure()
        print(np_to_date(inits.max()).isoformat())
        return

    if dataset is None or not start_time or not end_time:
        raise UsageError("--dataset, --start-time and --end-time are required.")
    if members and not dataset.startswith("ecmwf"):
        raise UsageError("--members applies to --dataset ecmwf and ecmwf_wb2 only.")
    table = _STORES[dataset]
    variables = list(dict.fromkeys(_ALIASES.get(v, v) for v in (variable or ["precip"])))
    unknown = [v for v in variables if v not in table]
    if unknown:
        raise UsageError(
            f"{dataset} has no variable {', '.join(unknown)}; choose from {', '.join(table)}."
        )

    parts = []
    for v in variables:
        path, name, units = table[v]
        ds = _open_store(path)[[name]].rename({name: v}).drop_vars("time", errors="ignore")
        # Core pairs the Nth --start-time with the Nth --end-time; an init in any window is kept once.
        day = ds["init_time"].values.astype("datetime64[D]")
        keep = np.any(
            [
                (day >= np.datetime64(s, "D")) & (day <= np.datetime64(e, "D"))
                for s, e in zip(start_time, end_time, strict=True)
            ],
            axis=0,
        )
        if not keep.any():
            raise DataError(
                f"no {dataset} {v} init in the given dates (the store runs {day.min()} to {day.max()})."
            )
        # Still lazy: only the chunks inside the inits and the bbox are read.
        ds = ds.isel(init_time=keep)
        ds = bbox_subset(ds, bbox) if bbox is not None else ensure_normalized_longitude(ds)
        ds[v].attrs = {"units": units, "standard_name": _STANDARD_NAMES[v]}
        try:
            parts.append(ds.load())
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"failed to read {path} ({_redact(exc)}).") from None
    _close_azure()

    # Stores of one model can skip different inits: keep every init, NaN where a store has none.
    inits = np.unique(np.concatenate([p["init_time"].values for p in parts]))
    for p, v in zip(parts, variables, strict=True):
        missing = np.setdiff1d(inits, p["init_time"].values)
        if missing.size:
            print(
                f"hindcast-fetch: WARNING: {dataset} {v} has no run for {missing.size} init(s), "
                "left NaN: " + ", ".join(str(np_to_date(t)) for t in missing),
                file=sys.stderr,
            )
    try:
        cube = xr.merge([p.reindex(init_time=inits) for p in parts], join="exact")
    except ValueError as exc:
        raise DataError(
            f"{dataset} stores for {', '.join(variables)} are on different grids ({exc})."
        ) from None

    cube = cube.rename(
        init_time="time",
        member="number",
        prediction_timedelta="step",
        lat="latitude",
        lon="longitude",
    )
    cube = cube.transpose("time", "number", "step", "latitude", "longitude")
    if dataset.startswith("ecmwf") and not members:
        cube = cube.mean("number", keep_attrs=True)
    cube = to_standard_units(cube, variables=variables)
    cube.attrs = {
        "Conventions": "CF-1.13",
        "weather_skills_source": "+".join(table[v][0] for v in variables),
        "hindcast_dataset": dataset,
    }
    cube["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    cube["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    stamp_cf_attrs(cube)
    # Step d is the day [init + d, init + d + 1).
    return stamp_data_interval(cube, period="1 day", dim="step")


if __name__ == "__main__":
    fetch()

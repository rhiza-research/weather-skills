# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "gcsfs",
# ]
# ///
"""Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts for a range of init dates as a weather-skills Zarr."""
# Every source is a Sheerwater hindcast Zarr in 6° lat/lon tiles, one variable per
# store, dims (init_time, member, prediction_timedelta, lat, lon) in some order,
# prediction_timedelta in whole days from 0:
#   ecmwf — public. WeatherBench2's IFS extended-range reforecast members,
#     flattened to one run per init date 1996-2021: the newest issue that re-ran
#     that date, so the newest model that ran it.
#   gefs — public. CHC's GEFSv12 reforecast, Wednesday inits 1998-2019.
#   cumulus — private. The gratis-czajkowski Cumulus run at 1°, inits every
#     second day 2016-2024; member 0 is the cf run.
# All become one multi-init cube (time=init, number, step, latitude, longitude).

from __future__ import annotations

import json
import os
import sys

import numpy as np
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude, np_to_date
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_PUBLIC = "gs://sheerwater-public-datalake/hindcasts"
_PRIVATE = "gs://sheerwater-datalake/hindcasts"
# --dataset -> -v name -> (store, store variable, units). The stores' own unit
# strings ("C", "mm / day") are not all pint-readable, so units are set here.
_STORES = {
    "ecmwf": {
        "precip": (f"{_PUBLIC}/ecmwf_ifs_precip.zarr", "precip", "mm day-1"),
        "tmp2m": (f"{_PUBLIC}/ecmwf_ifs_tmp2m.zarr", "tmp2m", "degree_Celsius"),
    },
    "gefs": {
        "precip": (f"{_PUBLIC}/gefs_precip.zarr", "precip", "mm day-1"),
        "tmp2m": (f"{_PUBLIC}/gefs_tmp2m.zarr", "tmp2m", "degree_Celsius"),
    },
    "cumulus": {
        "precip": (f"{_PRIVATE}/cumulus_precip.zarr", "precip", "mm day-1"),
        "tmp2m": (f"{_PRIVATE}/cumulus_tmp2m.zarr", "tmp2m", "K"),
    },
}
_ALIASES = {"tp": "precip", "precipitation": "precip", "t2m": "tmp2m"}
_STANDARD_NAMES = {"precip": "lwe_precipitation_rate", "tmp2m": "air_temperature"}
_AUTH_MSG = (
    f"{_PRIVATE} is private. Set NEURAL_GCM_SERVICE_CREDENTIALS to the full service-account "
    "key JSON, GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON file path, or run "
    "`gcloud auth application-default login`."
)


def _open_store(path: str) -> xr.Dataset:
    """Open a hindcast store: public ones anonymously, private ones with Google credentials."""
    if path.startswith(_PUBLIC):
        try:
            return xr.open_zarr(path, storage_options={"token": "anon"})
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"failed to open {path} ({exc}).") from None

    import gcsfs

    creds_json = os.environ.get("NEURAL_GCM_SERVICE_CREDENTIALS")
    try:
        token = json.loads(creds_json) if creds_json else None
    except json.JSONDecodeError as exc:
        raise DataError(f"NEURAL_GCM_SERVICE_CREDENTIALS is not valid JSON ({exc}).") from None
    try:
        return xr.open_zarr(gcsfs.GCSFileSystem(token=token).get_mapper(path.removeprefix("gs://")))
    except Exception as exc:  # noqa: BLE001 — surface auth failures cleanly
        raise DataError(f"{_AUTH_MSG} Opening {path} failed: {exc}") from None


@weather_skill(name="hindcast-fetch", version=_SKILL_VERSION)
@weather_skill.argument(
    "--dataset", choices=list(_STORES), help=f"Hindcast source: {', '.join(_STORES)}."
)
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--members",
    action="store_true",
    help="ecmwf only: the 10 perturbed members instead of their mean.",
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
        print(np_to_date(inits.max()).isoformat())
        return

    if dataset is None or start_time is None or end_time is None:
        raise UsageError("--dataset, --start-time and --end-time are required.")
    if start_time > end_time:
        raise UsageError(f"--start-time {start_time} is after --end-time {end_time}.")
    if dataset != "ecmwf" and members:
        raise UsageError(
            "--members applies to --dataset ecmwf only; gefs and cumulus always have all members."
        )
    table = _STORES[dataset]
    variables = list(dict.fromkeys(_ALIASES.get(v, v) for v in (variable or ["precip"])))
    unknown = [v for v in variables if v not in table]
    if unknown:
        raise UsageError(
            f"{dataset} hindcasts have no variable {', '.join(unknown)}; "
            f"choose from {', '.join(table)}."
        )

    start = np.datetime64(start_time)
    end = np.datetime64(end_time) + np.timedelta64(1, "D")
    parts = []
    for v in variables:
        path, source, units = table[v]
        ds = _open_store(path)
        inits = ds["init_time"].values
        keep = np.nonzero((inits >= start) & (inits < end))[0]
        if keep.size == 0:
            raise DataError(
                f"no {dataset} {v} hindcast init between {start_time} and {end_time} (the store "
                f"runs {np_to_date(inits.min())} to {np_to_date(inits.max())})."
            )
        # A store's extra `time` coord, where it has one, repeats init_time.
        part = ds[[source]].drop_vars("time", errors="ignore").isel(init_time=keep)
        part = part.rename({source: v})
        part[v].attrs = {"units": units}
        # Cut to the bbox and load before aligning inits: reindexing a lazy store
        # merges its lat/lon tiles and would read the whole globe.
        part = bbox_subset(part, bbox) if bbox is not None else ensure_normalized_longitude(part)
        parts.append(part.load())
    # Stores of one model can skip different inits; keep every init and leave a
    # variable NaN where its store has no run. Never filled: say which.
    # Only init_time may differ: the grids and members must match exactly.
    inits = np.unique(np.concatenate([part["init_time"].values for part in parts]))
    try:
        cube = xr.merge([part.reindex(init_time=inits) for part in parts], join="exact")
    except ValueError as exc:
        raise DataError(
            f"{dataset} hindcast stores for {', '.join(variables)} are on different grids "
            f"({exc}); fetch them in separate calls."
        ) from None
    for part, v in zip(parts, variables, strict=True):
        missing = np.setdiff1d(inits, part["init_time"].values)
        if missing.size:
            print(
                f"hindcast-fetch: WARNING: {dataset} {v} has no run for {missing.size} init(s), "
                "left NaN: " + ", ".join(str(np_to_date(t)) for t in missing),
                file=sys.stderr,
            )
    cube = cube.rename(
        init_time="time",
        member="number",
        prediction_timedelta="step",
        lat="latitude",
        lon="longitude",
    )
    # Daily leads: step d covers [init + d, init + d + 1 day). Stores keep steps
    # as whole days, as integers or already decoded to timedelta.
    steps = cube["step"].values
    if not np.issubdtype(steps.dtype, np.timedelta64):
        steps = steps.astype("int64").astype("timedelta64[D]")
    cube = cube.assign_coords(
        number=cube["number"].values.astype("int64"), step=steps.astype("timedelta64[ns]")
    )
    cube = cube.transpose("time", "number", "step", "latitude", "longitude")
    if dataset == "ecmwf" and not members:
        # WeatherBench2's ensemble mean is the mean of these 10 members.
        cube = cube.mean("number", keep_attrs=True)
    print(
        f"hindcast-fetch: {dataset} {cube.sizes['time']} inits "
        f"{np_to_date(cube['time'].values[0])}..{np_to_date(cube['time'].values[-1])} from "
        + ", ".join(table[v][0] for v in variables),
        file=sys.stderr,
    )
    if "precip" in cube and float(cube["precip"].min()) < -0.1:
        # Some model output is slightly negative; kept as published, never clipped.
        print(
            f"hindcast-fetch: WARNING: precip goes down to {float(cube['precip'].min()):.2f} "
            "mm day-1; kept as published (not clipped).",
            file=sys.stderr,
        )

    # Names like tmp2m are not recognized by to_standard_units; the CF name is.
    for v in variables:
        cube[v].attrs["standard_name"] = _STANDARD_NAMES[v]
    cube = to_standard_units(cube, variables=variables)
    cube.attrs = {
        "Conventions": "CF-1.13",
        "weather_skills_source": "sheerwater:"
        + "+".join(table[v][0].removeprefix("gs://") for v in variables),
        "hindcast_dataset": dataset,
    }
    cube["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    cube["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    stamp_cf_attrs(cube)
    return stamp_data_interval(cube, period="1 day", dim="step")


if __name__ == "__main__":
    fetch()

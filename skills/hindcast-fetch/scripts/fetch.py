# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "gcsfs",
#   "pandas",
# ]
# ///
"""Fetch ECMWF or GEFS subseasonal hindcasts (reforecasts) for a range of init dates as a weather-skills Zarr."""
# The two hindcast stores Sheerwater reads:
#   ecmwf — WeatherBench2 IFS extended-range reforecast (public). Dims
#     (forecast_time, [number,] hindcast_year, prediction_timedelta, latitude,
#     longitude); forecast_time is the issuance date (2016–2022, Mon/Thu) and the
#     2-D ``time`` coord holds the real init date of each of its 20 hindcast years.
#   gefs — Sheerwater's GEFS reforecast cache (private, Zarr v3). Dims
#     (init_time, member, prediction_timedelta, lat, lon); init_time is a real
#     Wednesday init in days since 1960-01-01, stored undecoded.
# Both become one multi-init cube (time=init, [number,] step, latitude, longitude).

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_ECMWF_ROOT = "gs://weatherbench2/datasets/ifs_extended_range/daily"
_ECMWF_MEAN = f"{_ECMWF_ROOT}/ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr"
_ECMWF_MEMBERS = f"{_ECMWF_ROOT}/ifs-ext-reforecast-full-single-level-avg.zarr"
_GEFS_STORE = "sheerwater-datalake/caches/gefs_chc_raw/global1_0_{variable}.zarr"

# -v token -> (source variable, source units). The WeatherBench2 store carries no
# units; precip is a 24 h total in metres, temperatures are daily K.
_ECMWF_VARIABLES = {
    "precip": ("total_precipitation_24hr", "m day-1"),
    "tmp2m": ("2m_temperature", "K"),
    "tmax2m": ("2m_temperature_max", "K"),
    "tmin2m": ("2m_temperature_min", "K"),
}
# GEFS tmp2m is stamped "C", which pint reads as coulomb.
_GEFS_VARIABLES = {
    "precip": ("precip", "mm day-1"),
    "tmp2m": ("tmp2m", "degree_Celsius"),
}

_AUTH_MSG = (
    "cannot read gs://sheerwater-datalake/caches/gefs_chc_raw (private GCS). Set "
    "NEURAL_GCM_SERVICE_CREDENTIALS to the full service-account key JSON "
    "(no file needed), GOOGLE_APPLICATION_CREDENTIALS to a service-account "
    "JSON file path, or run `gcloud auth application-default login`."
)


def _gcs():
    """Authenticated GCS filesystem: NEURAL_GCM_SERVICE_CREDENTIALS, then
    GOOGLE_APPLICATION_CREDENTIALS / Application Default Credentials.
    """
    import gcsfs

    creds_json = os.environ.get("NEURAL_GCM_SERVICE_CREDENTIALS")
    try:
        token = json.loads(creds_json) if creds_json else None
    except json.JSONDecodeError as exc:
        raise DataError(f"NEURAL_GCM_SERVICE_CREDENTIALS is not valid JSON ({exc}).") from None
    try:
        return gcsfs.GCSFileSystem(token=token)
    except Exception as exc:  # noqa: BLE001 — surface auth failures cleanly
        raise DataError(f"{_AUTH_MSG} ({exc})") from None


def _open_ecmwf(members: bool) -> xr.Dataset:
    path = _ECMWF_MEMBERS if members else _ECMWF_MEAN
    try:
        return xr.open_zarr(path, storage_options={"token": "anon"}, decode_timedelta=True)
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed to open {path} ({exc}).") from None


def _open_gefs(variable: str) -> xr.Dataset:
    path = _GEFS_STORE.format(variable=variable)
    try:
        mapper = _gcs().get_mapper(path)
        ds = xr.open_zarr(mapper, zarr_format=3, consolidated=False, decode_times=False)
    except DataError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"{_AUTH_MSG} Opening gs://{path} failed: {exc}") from None
    # init_time is float days since 1960-01-01; decode by hand (no CF decode on v3 here).
    inits = pd.Timestamp("1960-01-01") + pd.to_timedelta(
        ds["init_time"].values.astype("float64"), "D"
    )
    return ds.assign_coords(init_time=inits.values.astype("datetime64[ns]"))


def _ecmwf_cube(ds, variables, start, end, issue_year, bbox):
    """Pick the (issuance, hindcast year) pairs of one issue year whose real init
    falls in [start, end] and lay them on a single ``time`` axis."""
    issued = pd.DatetimeIndex(ds["forecast_time"].values)
    issue_years = sorted(set(issued.year))
    if issue_year is None:
        # Latest issue year whose 20 hindcast years cover the whole window.
        fits = [y for y in issue_years if y - 20 <= start.year and y - 1 >= end.year]
        if not fits:
            raise UsageError(
                f"no single ECMWF issue year covers inits {start}..{end}: issue years "
                f"{issue_years[0]}–{issue_years[-1]} each carry the 20 years before them "
                f"(inits {issue_years[0] - 20}–{issue_years[-1] - 1} overall). Narrow the "
                "window or pass --issue-year."
            )
        issue_year = fits[-1]
    elif issue_year not in issue_years:
        raise UsageError(
            f"--issue-year {issue_year} not in the store; available: "
            f"{issue_years[0]}–{issue_years[-1]}."
        )

    inits = ds["time"].values  # (forecast_time, hindcast_year)
    in_year = (issued.year == issue_year)[:, None]
    in_window = (inits >= np.datetime64(start)) & (
        inits < np.datetime64(end) + np.timedelta64(1, "D")
    )
    fi, hi = np.nonzero(in_year & in_window)
    if fi.size == 0:
        raise DataError(
            f"issue year {issue_year} has no hindcast init between {start} and {end} "
            f"(its inits run {issue_year - 20}-01 to {issue_year - 1}-12)."
        )
    sources = [_ECMWF_VARIABLES[v][0] for v in variables]
    region = ds[sources].drop_vars(["time", "valid_time"])
    region = bbox_subset(region, bbox) if bbox is not None else ensure_normalized_longitude(region)
    # Load one issue at a time with plain indexing: a vectorized pick across
    # issues and years holds a whole globe per init in memory before the bbox.
    pieces = []
    for f in np.unique(fi):
        h = hi[fi == f]
        piece = region.isel(forecast_time=f, hindcast_year=h).load().drop_vars("forecast_time")
        piece = piece.rename({"hindcast_year": "time"}).assign_coords(
            time=inits[f, h], issue_time=("time", np.repeat(issued.values[f], h.size))
        )
        pieces.append(piece)
    cube = xr.concat(pieces, "time").sortby("time")
    cube = cube.rename({"prediction_timedelta": "step"})
    for v in variables:
        source, units = _ECMWF_VARIABLES[v]
        cube = cube.rename({source: v})
        cube[v].attrs["units"] = units
    return cube, issue_year


def _gefs_cube(variables, start, end):
    parts = []
    for v in variables:
        ds = _open_gefs(v)
        keep = (ds["init_time"].values >= np.datetime64(start)) & (
            ds["init_time"].values < np.datetime64(end) + np.timedelta64(1, "D")
        )
        if not keep.any():
            first, last = pd.DatetimeIndex(ds["init_time"].values[[0, -1]]).strftime("%Y-%m-%d")
            raise DataError(
                f"no GEFS hindcast init between {start} and {end} (store runs {first} to {last})."
            )
        ds = ds[[v]].isel(init_time=np.nonzero(keep)[0])
        ds[v].attrs["units"] = _GEFS_VARIABLES[v][1]
        parts.append(ds)
    # The precip and tmp2m stores skip different weeks; keep every init and leave
    # a variable NaN where its store has no run. Never filled: say which.
    cube = xr.merge(parts, join="outer")
    for part, v in zip(parts, variables, strict=True):
        missing = np.setdiff1d(cube["init_time"].values, part["init_time"].values)
        if missing.size:
            print(
                f"hindcast-fetch: WARNING: GEFS {v} has no run for {missing.size} init(s), left NaN: "
                + ", ".join(pd.DatetimeIndex(missing).strftime("%Y-%m-%d")),
                file=sys.stderr,
            )
    cube = cube.rename(
        {
            "init_time": "time",
            "member": "number",
            "prediction_timedelta": "step",
            "lat": "latitude",
            "lon": "longitude",
        }
    )
    steps = pd.to_timedelta(cube["step"].values, "D").values.astype("timedelta64[ns]")
    return cube.assign_coords(number=cube["number"].values.astype("int64"), step=steps)


@weather_skill(name="hindcast-fetch", version=_SKILL_VERSION)
@weather_skill.argument(
    "--dataset",
    choices=["ecmwf", "gefs"],
    help="Hindcast source: ecmwf (IFS extended range, public) or gefs (Sheerwater GEFS cache, credentials).",
)
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--issue-year",
    type=int,
    help="ecmwf only: year whose model cycle issued the hindcasts (2016–2022). Default: the latest that covers the window.",
)
@weather_skill.argument(
    "--members",
    action="store_true",
    help="ecmwf only: fetch the 10 perturbed members instead of the ensemble mean.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="ecmwf",
    metavar="DATASET",
    probe=True,
    help="Print the latest hindcast init date (YYYY-MM-DD) for ecmwf (default) or gefs and exit.",
)
def fetch(
    dataset, start_time, end_time, variable, bbox, issue_year, members, probe_latest, **kwargs
):
    """Fetch ECMWF or GEFS subseasonal hindcasts (reforecasts) for a range of init dates as a weather-skills Zarr."""
    if probe_latest is not None:
        if probe_latest == "gefs":
            latest = _open_gefs("precip")["init_time"].values.max()
        elif probe_latest == "ecmwf":
            latest = _open_ecmwf(members=False)["time"].values.max()
        else:
            raise UsageError(f"--probe-latest takes ecmwf or gefs; got {probe_latest!r}.")
        print(pd.Timestamp(latest).strftime("%Y-%m-%d"))
        return

    if dataset is None or start_time is None or end_time is None:
        raise UsageError("--dataset, --start-time and --end-time are required.")
    table = _ECMWF_VARIABLES if dataset == "ecmwf" else _GEFS_VARIABLES
    variables = variable or ["precip"]
    unknown = [v for v in variables if v not in table]
    if unknown:
        raise UsageError(
            f"{dataset} has no variable {', '.join(unknown)}; choose from {', '.join(table)}."
        )

    if dataset == "ecmwf":
        cube, issue_year = _ecmwf_cube(
            _open_ecmwf(members), variables, start_time, end_time, issue_year, bbox
        )
        source = "weatherbench2:ifs-ext-reforecast" + ("" if members else "-ens-mean")
    else:
        if issue_year is not None or members:
            raise UsageError(
                "--issue-year and --members apply to --dataset ecmwf only; gefs always has all 11 members."
            )
        cube = _gefs_cube(variables, start_time, end_time)
        cube = bbox_subset(cube, bbox) if bbox is not None else ensure_normalized_longitude(cube)
        source = "sheerwater:gefs_chc_raw"

    print(
        f"hindcast-fetch: {dataset} {cube.sizes['time']} inits "
        f"{pd.Timestamp(cube['time'].values[0]):%Y-%m-%d}..{pd.Timestamp(cube['time'].values[-1]):%Y-%m-%d}"
        + (f" from issue year {issue_year}" if dataset == "ecmwf" else ""),
        file=sys.stderr,
    )
    cube = cube.transpose("time", ..., "step", "latitude", "longitude").load()
    # Names like tmp2m are not recognized by to_standard_units; the CF name is.
    for v in variables:
        if v != "precip":
            cube[v].attrs["standard_name"] = "air_temperature"
    cube = to_standard_units(cube, variables=variables)

    # WeatherBench2 derives daily precip by differencing accumulations, and the
    # IFS extended-range split at day 15 leaves some negative day-15 values.
    # Kept as published, never clipped: say where they are.
    if "precip" in variables:
        negative = (cube["precip"] < -0.1).any([d for d in cube["precip"].dims if d != "step"])
        if negative.any():
            days = (cube["step"].values[negative.values] // np.timedelta64(1, "D")).tolist()
            print(
                f"hindcast-fetch: WARNING: precip has values below -0.1 mm day-1 at lead day(s) "
                f"{days}; kept as published (not clipped).",
                file=sys.stderr,
            )

    cube.attrs = {
        "Conventions": "CF-1.13",
        "weather_skills_source": source,
        "hindcast_dataset": dataset,
    }
    if dataset == "ecmwf":
        cube.attrs["hindcast_issue_year"] = issue_year
    cube["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    cube["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    stamp_cf_attrs(cube)
    return stamp_data_interval(cube, period="1 day")


if __name__ == "__main__":
    fetch()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "gcsfs",
#   "adlfs",
#   "h5netcdf",
#   "h5py",
# ]
# ///
"""Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts for a range of init dates as a weather-skills Zarr."""
# Sources:
#   ecmwf — WeatherBench2 IFS extended-range reforecast (public). Dims
#     (forecast_time, [number,] hindcast_year, prediction_timedelta, latitude,
#     longitude); forecast_time is the issue date (2016-2022, Mon/Thu) and the
#     2-D ``time`` coord holds the real init date of each of its 20 hindcast
#     years. precip also comes from Sheerwater's public copy of the members,
#     flattened to one run per init date (the newest issue that ran it) and
#     tiled, whenever every run asked for is in it.
#   gefs — Sheerwater's GEFSv12 reforecast: precip public (decoded), tmp2m the
#     private Zarr v3 cache with init_time in days since 1960-01-01. Dims
#     (init_time, member, prediction_timedelta, lat, lon), Wednesday inits.
#   cumulus — Azure NetCDF per init and lead, control (cf) and perturbed (pf)
#     members apart: <root>/{cf,pf}/<folder>/data/YYYY-MM-DD-HH-LLLL.nc, each
#     lead the 24 h total ending at init + LLLL hours.
# All become one multi-init cube (time=init, [number,] step, latitude, longitude).

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

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

_ECMWF_ROOT = "gs://weatherbench2/datasets/ifs_extended_range/daily"
_ECMWF_MEAN = f"{_ECMWF_ROOT}/ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr"
_ECMWF_MEMBERS = f"{_ECMWF_ROOT}/ifs-ext-reforecast-full-single-level-avg.zarr"
_ECMWF_STORE = "gs://sheerwater-public-datalake/hindcasts/ecmwf_ifs_precip.zarr"
# The store holds inits 2016-01-01..2021-12-29: the newest issue's run of each.
_ECMWF_STORE_START = np.datetime64("2016-01-01", "ns")
# -v name -> (WeatherBench2 field, units). The store carries no units: precip is
# a 24 h total in metres, temperatures are 24 h mean / max / min in K.
_ECMWF_VARIABLES = {
    "precip": ("total_precipitation_24hr", "m day-1"),
    "tmp2m": ("2m_temperature", "K"),
    "tmax2m": ("2m_temperature_max", "K"),
    "tmin2m": ("2m_temperature_min", "K"),
    "d2m": ("2m_dewpoint_temperature", "K"),
    "sst": ("sea_surface_temperature", "K"),
}
# -v name -> (store, store variable, units). GEFS tmp2m is stamped "C", which
# pint reads as coulomb.
_GEFS_VARIABLES = {
    "precip": ("gs://sheerwater-public-datalake/hindcasts/gefs_precip.zarr", "precip", "mm day-1"),
    "tmp2m": (
        "sheerwater-datalake/caches/gefs_chc_raw/global1_0_tmp2m.zarr",
        "tmp2m",
        "degree_Celsius",
    ),
}
_GEFS_AUTH_MSG = (
    "GEFS tmp2m hindcasts need gs://sheerwater-datalake/caches/gefs_chc_raw (private GCS). "
    "Set NEURAL_GCM_SERVICE_CREDENTIALS to the full service-account key JSON, "
    "GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON file path, or run "
    "`gcloud auth application-default login`."
)

_AZURE_ACCOUNT = "italynorthdata"
_SAS_ENV = "AZURE_STORAGE_SAS_TOKEN"
_CUMULUS_RUN = "gratis-czajkowski"
_CUMULUS_ROOT = f"data/ml_reforecasts/{_CUMULUS_RUN}"
_CUMULUS_LEADS = [f"{24 * (i + 1):04d}" for i in range(46)]
_CUMULUS_FIRST_YEAR = 2016
# --dataset -> variable folder (IMERG-trained 0.25°, IMERG-trained 1°, ERA5-trained).
_CUMULUS_FOLDERS = {
    "cumulus": "total_precipitation_24h_acc_imerg_0p25",
    "cumulus_1deg": "total_precipitation_24h_acc_imerg",
    "cumulus_era5": "total_precipitation_24h_acc_era5",
}

_DATASETS = ["ecmwf", "gefs", *_CUMULUS_FOLDERS]
_ALIASES = {"tp": "precip", "precipitation": "precip", "t2m": "tmp2m"}
_STANDARD_NAMES = {
    "precip": "lwe_precipitation_rate",
    "tmp2m": "air_temperature",
    "tmax2m": "air_temperature",
    "tmin2m": "air_temperature",
    "d2m": "dew_point_temperature",
    "sst": "sea_surface_temperature",
}
DEFAULT_WORKERS = 8


def _open_ecmwf(members: bool) -> xr.Dataset:
    path = _ECMWF_MEMBERS if members else _ECMWF_MEAN
    try:
        return xr.open_zarr(path, storage_options={"token": "anon"}, decode_timedelta=True)
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed to open {path} ({exc}).") from None


def _open_ecmwf_store() -> xr.Dataset:
    try:
        ds = xr.open_zarr(_ECMWF_STORE, storage_options={"token": "anon"})
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed to open {_ECMWF_STORE} ({exc}).") from None
    return ds


def _open_gefs(name: str) -> xr.Dataset:
    """Open one GEFS hindcast store with init_time decoded to datetime64."""
    path, _, _ = _GEFS_VARIABLES[name]
    if name == "precip":
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
        mapper = gcsfs.GCSFileSystem(token=token).get_mapper(path)
        ds = xr.open_zarr(mapper, zarr_format=3, consolidated=False, decode_times=False)
    except Exception as exc:  # noqa: BLE001 — surface auth failures cleanly
        raise DataError(f"{_GEFS_AUTH_MSG} Opening gs://{path} failed: {exc}") from None
    days = np.rint(ds["init_time"].values.astype("float64")).astype("int64")
    inits = np.datetime64("1960-01-01", "ns") + days.astype("timedelta64[D]")
    return ds.assign_coords(init_time=inits)


def _azure_options() -> dict[str, str]:
    """adlfs options from the SAS in the env (raw query, leading ``?``, or full blob URL)."""
    (raw,) = require_env(
        _SAS_ENV,
        message=(
            f"{_SAS_ENV} must be set to a read SAS for Azure container "
            f"{_AZURE_ACCOUNT}/data (list and read: sp=rl)."
        ),
    )
    token = raw.strip().strip("'\"").split("?", 1)[-1]
    if "sig=" not in token.lower():
        raise UsageError(
            f"{_SAS_ENV} is set but does not look like an Azure SAS "
            "(expected a query string containing sig=)."
        )
    return {"account_name": _AZURE_ACCOUNT, "sas_token": token}


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
        hint = f" Check {_SAS_ENV}: it needs list and read (sp=rl) on {_AZURE_ACCOUNT}/data."
    return DataError(f"{what} ({text}).{hint}")


def _list_cumulus_inits(folder: str, prefix: str) -> list[str]:
    """``YYYY-MM-DD-HH`` Cumulus hindcast inits whose name starts with ``prefix``."""
    options = _azure_options()  # check the SAS before importing adlfs
    import adlfs

    fs = adlfs.AzureBlobFileSystem(**options)
    data = f"{_CUMULUS_ROOT}/pf/{folder}/data"
    try:
        paths = fs.find(data, prefix=prefix)
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(exc, f"Azure listing failed for az://{_AZURE_ACCOUNT}/{data}/") from None
    return sorted({path.rsplit("/", 1)[-1][:13] for path in paths if path.endswith(".nc")})


def _open_cumulus(folder: str, init: str, bbox, workers: int) -> xr.DataArray:
    """One Cumulus hindcast init as (number, step, latitude, longitude) 24 h totals.

    Built like the live Zarr export: the ``cf`` file is member 0 ahead of the
    29 ``pf`` members (1-29), and negative model output is set to 0.
    """
    import io
    from concurrent.futures import ThreadPoolExecutor

    import adlfs

    fs = adlfs.AzureBlobFileSystem(**_azure_options())

    def read(path):
        try:
            raw = fs.cat_file(path)
        except FileNotFoundError:
            raise DataError(
                f"Cumulus hindcast init {init} is missing az://{_AZURE_ACCOUNT}/{path}; "
                f"every init should have all {len(_CUMULUS_LEADS)} leads. Leave it out of "
                "--start-time..--end-time."
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise _azure_error(exc, f"failed to read az://{_AZURE_ACCOUNT}/{path}") from None
        with xr.open_dataset(io.BytesIO(raw), engine="h5netcdf") as ds:
            return (bbox_subset(ds, bbox) if bbox is not None else ds)[folder].load()

    paths = [
        f"{_CUMULUS_ROOT}/{ens}/{folder}/data/{init}-{lead}.nc"
        for lead in _CUMULUS_LEADS
        for ens in ("cf", "pf")
    ]
    with ThreadPoolExecutor(workers) as pool:
        fields = list(pool.map(read, paths))
    # fields alternate cf, pf per lead; cf has no member dim.
    leads = [
        xr.concat([cf.expand_dims(number=[0]), pf], "number")
        for cf, pf in zip(fields[::2], fields[1::2], strict=True)
    ]
    da = xr.concat(leads, "step").clip(min=0)
    # Each lead is the 24 h total ending at init + lead; label it by the period start.
    steps = [np.timedelta64(int(lead) - 24, "h") for lead in _CUMULUS_LEADS]
    da = da.assign_coords(step=np.array(steps, dtype="timedelta64[ns]"))
    return da.expand_dims(time=[np.datetime64(f"{init[:10]}T{init[11:]}", "ns")])


def _ecmwf_issue_pairs(wb2, start, end, issue_year):
    """(issue index, hindcast-year index) pairs of one issue year with inits in [start, end]."""
    issued = wb2["forecast_time"].values
    years = issued.astype("datetime64[Y]").astype(int) + 1970
    issue_years = sorted(set(years.tolist()))
    if issue_year is None:
        # Latest issue year whose 20 hindcast years cover the whole window.
        fits = [y for y in issue_years if y - 20 <= start.year and y - 1 >= end.year]
        if not fits:
            raise UsageError(
                f"no single ECMWF issue year covers inits {start}..{end}: issue years "
                f"{issue_years[0]}-{issue_years[-1]} each carry the 20 years before them "
                f"(inits {issue_years[0] - 20}-{issue_years[-1] - 1} overall). Narrow the "
                "window or pass --issue-year."
            )
        issue_year = fits[-1]
    elif issue_year not in issue_years:
        raise UsageError(
            f"--issue-year {issue_year} not in the store; available: "
            f"{issue_years[0]}-{issue_years[-1]}."
        )
    inits = wb2["time"].values  # (forecast_time, hindcast_year)
    in_window = (inits >= np.datetime64(start)) & (
        inits < np.datetime64(end) + np.timedelta64(1, "D")
    )
    fi, hi = np.nonzero((years == issue_year)[:, None] & in_window)
    if fi.size == 0:
        raise DataError(
            f"issue year {issue_year} has no hindcast init between {start} and {end} "
            f"(its inits run {issue_year - 20}-01 to {issue_year - 1}-12)."
        )
    return fi, hi, issue_year


def _fetch_ecmwf(variables, start, end, issue_year, members, bbox):
    wb2 = _open_ecmwf(members)
    issued = wb2["forecast_time"].values
    inits = wb2["time"].values  # (forecast_time, hindcast_year)
    # The store keeps, for each init date, the run of the newest issue that ran it.
    flat_inits = inits.ravel()
    flat_issued = np.repeat(issued, inits.shape[1])
    order = np.lexsort((flat_issued, flat_inits))
    last = np.r_[flat_inits[order][1:] != flat_inits[order][:-1], True]
    newest = dict(zip(flat_inits[order][last], flat_issued[order][last], strict=True))
    store_inits = [t for t in newest if t >= _ECMWF_STORE_START]
    store_end = max(store_inits)

    parts = []
    if (
        issue_year is None
        and variables == ["precip"]
        and np.datetime64(start) >= _ECMWF_STORE_START
    ):
        # Every init date in the window once, from the newest issue that ran it.
        times = np.array(
            sorted(
                t
                for t in store_inits
                if np.datetime64(start) <= t < np.datetime64(end) + np.timedelta64(1, "D")
            ),
            dtype="datetime64[ns]",
        )
        if times.size == 0:
            raise DataError(f"no ECMWF hindcast init between {start} and {end}.")
        if np.datetime64(end) > store_end:
            print(
                f"WARNING: ECMWF hindcast inits end {np_to_date(store_end)}; "
                f"none after that up to {end}.",
                file=sys.stderr,
            )
        issue_times = np.array([newest[t] for t in times], dtype="datetime64[ns]")
        store_vars, wb2_vars, fi, hi = ["precip"], [], None, None
    else:
        fi, hi, issue_year = _ecmwf_issue_pairs(wb2, start, end, issue_year)
        times, issue_times = inits[fi, hi], issued[fi]
        # precip comes from the store when it holds every one of these runs.
        in_store = all(
            t >= _ECMWF_STORE_START and newest[t] == i
            for t, i in zip(times, issue_times, strict=True)
        )
        store_vars = ["precip"] if "precip" in variables and in_store else []
        wb2_vars = [v for v in variables if v not in store_vars]

    print(
        f"hindcast-fetch: ECMWF {times.size} inits "
        + (f"from issue year {issue_year}" if issue_year else "from the newest issue of each")
        + (f"; precip from {_ECMWF_STORE}" if store_vars else "")
        + (
            f"; {', '.join(wb2_vars)} from {_ECMWF_MEMBERS if members else _ECMWF_MEAN}"
            if wb2_vars
            else ""
        ),
        file=sys.stderr,
    )
    if store_vars:
        # The store's own `time` coord repeats init_time.
        part = _open_ecmwf_store()[["precip"]].drop_vars("time", errors="ignore")
        part = part.sel(init_time=np.sort(times))
        part = part.rename(
            init_time="time",
            member="number",
            prediction_timedelta="step",
            lat="latitude",
            lon="longitude",
        )
        part = bbox_subset(part, bbox) if bbox is not None else ensure_normalized_longitude(part)
        part = part.assign_coords(
            number=part["number"].values.astype("int64"),
            step=part["step"].values.astype("timedelta64[D]").astype("timedelta64[ns]"),
        ).load()
        if not members:
            # WeatherBench2's ensemble mean is the mean of these 10 members.
            part = part.mean("number")
        part["precip"].attrs = {"units": "mm day-1"}
        parts.append(part)
    if wb2_vars:
        region = wb2[[_ECMWF_VARIABLES[v][0] for v in wb2_vars]].drop_vars(["time", "valid_time"])
        region = (
            bbox_subset(region, bbox) if bbox is not None else ensure_normalized_longitude(region)
        )
        # Load one issue at a time with plain indexing: a vectorized pick across
        # issues and years holds a whole globe per init in memory before the bbox.
        pieces = []
        for f in np.unique(fi):
            h = hi[fi == f]
            piece = region.isel(forecast_time=f, hindcast_year=h).load().drop_vars("forecast_time")
            pieces.append(piece.rename(hindcast_year="time").assign_coords(time=inits[f, h]))
        part = xr.concat(pieces, "time").sortby("time").rename(prediction_timedelta="step")
        for v in wb2_vars:
            source, units = _ECMWF_VARIABLES[v]
            part = part.rename({source: v})
            if units == "K":
                part[v] = part[v] - 273.15
                units = "degree_Celsius"
            part[v].attrs = {"units": units}
        parts.append(part)
    try:
        cube = xr.merge(parts, join="exact")
    except ValueError as exc:
        raise DataError(
            f"ECMWF precip from {_ECMWF_STORE} and {', '.join(wb2_vars)} from WeatherBench2 "
            f"are on different grids ({exc}); fetch precip in a separate call."
        ) from None
    order = np.argsort(times)
    cube = cube.assign_coords(issue_time=("time", issue_times[order]))
    if "precip" in wb2_vars:
        # WeatherBench2 derives daily precip by differencing accumulations, and the
        # IFS extended-range split at day 15 leaves some negative day-15 values.
        # Kept as published, never clipped: say where they are.
        negative = (cube["precip"] < -1e-4).any([d for d in cube["precip"].dims if d != "step"])
        if negative.any():
            days = (cube["step"].values[negative.values] // np.timedelta64(1, "D")).tolist()
            print(
                f"hindcast-fetch: WARNING: precip has values below -0.1 mm day-1 at lead "
                f"day(s) {days}; kept as published (not clipped).",
                file=sys.stderr,
            )
    source = "+".join(
        (["sheerwater:hindcasts/ecmwf_ifs_precip"] if store_vars else [])
        + (
            ["weatherbench2:ifs-ext-reforecast" + ("" if members else "-ens-mean")]
            if wb2_vars
            else []
        )
    )
    return cube, source, issue_year


def _fetch_gefs(variables, start, end, bbox):
    parts = []
    for v in variables:
        ds = _open_gefs(v)
        inits = ds["init_time"].values
        keep = np.nonzero(
            (inits >= np.datetime64(start)) & (inits < np.datetime64(end) + np.timedelta64(1, "D"))
        )[0]
        if keep.size == 0:
            raise DataError(
                f"no GEFS {v} hindcast init between {start} and {end} (store runs "
                f"{np_to_date(inits.min())} to {np_to_date(inits.max())})."
            )
        part = ds[[_GEFS_VARIABLES[v][1]]].isel(init_time=keep).rename({_GEFS_VARIABLES[v][1]: v})
        part[v].attrs = {"units": _GEFS_VARIABLES[v][2]}
        parts.append(part)
    # The precip and tmp2m stores skip different weeks; keep every init and
    # leave a variable NaN where its store has no run. Never filled: say which.
    # Only init_time may differ: the grids and members must match exactly.
    inits = np.unique(np.concatenate([part["init_time"].values for part in parts]))
    try:
        cube = xr.merge([part.reindex(init_time=inits) for part in parts], join="exact")
    except ValueError as exc:
        raise DataError(
            f"GEFS hindcast stores for {', '.join(variables)} are on different grids ({exc}); "
            "fetch them in separate calls."
        ) from None
    for part, v in zip(parts, variables, strict=True):
        missing = np.setdiff1d(inits, part["init_time"].values)
        if missing.size:
            print(
                f"hindcast-fetch: WARNING: GEFS {v} has no run for {missing.size} init(s), "
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
    # Daily leads: step d covers [init + d, init + d + 1 day).
    cube = cube.assign_coords(
        number=cube["number"].values.astype("int64"),
        step=cube["step"].values.astype("int64").astype("timedelta64[D]").astype("timedelta64[ns]"),
    )
    cube = bbox_subset(cube, bbox) if bbox is not None else ensure_normalized_longitude(cube)
    source = "sheerwater:" + "+".join(
        "hindcasts/gefs_precip" if v == "precip" else "gefs_chc_raw/tmp2m" for v in variables
    )
    return cube, source


def _fetch_cumulus(dataset, start, end, bbox, workers):
    folder = _CUMULUS_FOLDERS[dataset]
    # A whole hindcast folder is ~45,000 files: list one year at a time.
    inits = [
        init
        for year in range(start.year, end.year + 1)
        for init in _list_cumulus_inits(folder, f"{year}-")
        if start.isoformat() <= init[:10] <= end.isoformat()
    ]
    prefix = f"az://{_AZURE_ACCOUNT}/{_CUMULUS_ROOT}/{{cf,pf}}/{folder}/data/"
    if not inits:
        raise DataError(
            f"no Cumulus hindcast init from {start} to {end} under {prefix}. "
            "Inits are every second day at 00 UTC, 2016-2024."
        )
    # Each init is read whole and cut to the bbox (~5.7 GB at 0.25°): one at a time.
    pieces = []
    for n, init in enumerate(inits, 1):
        print(f"hindcast-fetch: reading ({n}/{len(inits)}) {prefix}{init}-LLLL.nc", file=sys.stderr)
        pieces.append(_open_cumulus(folder, init, bbox, workers))
    cube = xr.concat(pieces, "time").to_dataset(name="precip")
    if bbox is None:
        cube = ensure_normalized_longitude(cube)
    # 24 h totals over a 1 day period: the same numbers as a mm day-1 rate.
    cube["precip"].attrs = {"units": "mm day-1"}
    return cube, f"cumulus:{_CUMULUS_RUN}/{folder}"


@weather_skill(name="hindcast-fetch", version=_SKILL_VERSION)
@weather_skill.argument(
    "--dataset", choices=_DATASETS, help=f"Hindcast source: {', '.join(_DATASETS)}."
)
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--issue-year",
    type=int,
    help="ecmwf only: year whose issues ran the hindcasts (2016-2022).",
)
@weather_skill.argument(
    "--members",
    action="store_true",
    help="ecmwf only: the 10 perturbed members instead of the ensemble mean.",
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"cumulus only: concurrent file downloads (default {DEFAULT_WORKERS}).",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="ecmwf",
    metavar="DATASET",
    probe=True,
    help="Print the latest hindcast init date (YYYY-MM-DD) for DATASET (default ecmwf) and exit.",
)
def fetch(
    dataset,
    start_time,
    end_time,
    variable,
    bbox,
    issue_year,
    members,
    workers,
    probe_latest,
    **kwargs,
):
    """Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts for a range of init dates as a weather-skills Zarr."""
    if probe_latest is not None:
        if probe_latest == "ecmwf":
            latest = np_to_date(_open_ecmwf(members=False)["time"].values.max())
        elif probe_latest == "gefs":
            latest = np_to_date(_open_gefs("precip")["init_time"].values.max())
        elif probe_latest in _CUMULUS_FOLDERS:
            latest = None
            for year in range(dt.date.today().year, _CUMULUS_FIRST_YEAR - 1, -1):
                inits = _list_cumulus_inits(_CUMULUS_FOLDERS[probe_latest], f"{year}-")
                if inits:
                    latest = inits[-1][:10]
                    break
        else:
            raise UsageError(
                f"--probe-latest takes one of {', '.join(_DATASETS)}; got {probe_latest!r}."
            )
        print(latest or "none")
        return

    if dataset is None or start_time is None or end_time is None:
        raise UsageError("--dataset, --start-time and --end-time are required.")
    if start_time > end_time:
        raise UsageError(f"--start-time {start_time} is after --end-time {end_time}.")
    if dataset != "ecmwf" and (issue_year is not None or members):
        raise UsageError("--issue-year and --members apply to --dataset ecmwf only.")
    if workers < 1:
        raise UsageError(f"--workers must be at least 1 (got {workers}).")
    table = {"ecmwf": _ECMWF_VARIABLES, "gefs": _GEFS_VARIABLES}.get(dataset, {"precip": None})
    variables = list(dict.fromkeys(_ALIASES.get(v, v) for v in (variable or ["precip"])))
    unknown = [v for v in variables if v not in table]
    if unknown:
        raise UsageError(
            f"{dataset} hindcasts have no variable {', '.join(unknown)}; choose from {', '.join(table)}."
        )

    issue_year_used = None
    if dataset == "ecmwf":
        cube, source, issue_year_used = _fetch_ecmwf(
            variables, start_time, end_time, issue_year, members, bbox
        )
    elif dataset == "gefs":
        cube, source = _fetch_gefs(variables, start_time, end_time, bbox)
    else:
        cube, source = _fetch_cumulus(dataset, start_time, end_time, bbox, workers)

    cube = cube.transpose("time", ..., "step", "latitude", "longitude").load()
    print(
        f"hindcast-fetch: {dataset} {cube.sizes['time']} inits "
        f"{np_to_date(cube['time'].values[0])}..{np_to_date(cube['time'].values[-1])}",
        file=sys.stderr,
    )
    # Names like tmp2m are not recognized by to_standard_units; the CF name is.
    for v in variables:
        cube[v].attrs["standard_name"] = _STANDARD_NAMES[v]
    cube = to_standard_units(cube, variables=variables)
    cube.attrs = {
        "Conventions": "CF-1.13",
        "weather_skills_source": source,
        "hindcast_dataset": dataset,
    }
    if issue_year_used is not None:
        cube.attrs["hindcast_issue_year"] = issue_year_used
    cube["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    cube["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    stamp_cf_attrs(cube)
    return stamp_data_interval(cube, period="1 day", dim="step")


if __name__ == "__main__":
    fetch()

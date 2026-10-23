# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "dynamical-catalog==1.0.1",
#   "gcsfs",
#   "pandas",
# ]
# ///
"""Fetch a forecast or analysis from the dynamical.org catalog as a weather-skills Zarr."""

import re
import sys

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    np_to_date,
)
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

# Forecast bookkeeping / CRS scalar coords dropped from the standard output.
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

# Catalog stores selected pressure-level fields as separate 2-D variables
# (`temperature_850hpa`). Stack those onto the ontology `vertical` dim.
_HPA_RE = re.compile(r"^(.+)_(\d+)hpa$")
_HPA_ALIASES = {
    "t": "temperature",
    "gh": "geopotential_height",
    "u": "wind_u",
    "v": "wind_v",
    "w": "vertical_velocity",
    "q": "specific_humidity",
}
# Other fetchers (TAHMO, ARCO, S2S) use these; dynamical.org stores surface precip as
# precipitation_surface (IMERG, GEFS, IFS-ENS, …).
_PRECIP_ALIASES = {
    "precip": "precipitation_surface",
    "precipitation": "precipitation_surface",
    "tp": "precipitation_surface",
    "total_precipitation": "precipitation_surface",
    "pr": "precipitation_surface",
}
# 46-day S2S/ER daily-mean surface names (and common S2S short names).
_SURFACE_ALIASES = {
    "t2m": "average_temperature_2m",
    "2m_temperature": "average_temperature_2m",
    "2_m_temperature": "average_temperature_2m",
    "sst": "sea_surface_temperature",
    "d2m": "average_dew_point_temperature_2m",
    "msl": "pressure_reduced_to_mean_sea_level",
    "sp": "pressure_surface",
    "u10": "wind_u_10m",
    "v10": "wind_v_10m",
    "u100": "wind_u_100m",
    "v100": "wind_v_100m",
}
_PRESSURE_GROUP = "pressure_level"

# GEFS hindcasts are not in the catalog: they come from Sheerwater's GEFSv12
# reforecast stores (Wednesday inits 1998-12-30..2019-08-28, 11 members,
# 35 daily leads, 1°), dims (init_time, member, prediction_timedelta, lat, lon).
# precip is a public, rechunked copy with init_time decoded; tmp2m is the
# private Zarr v3 cache with init_time in days since 1960-01-01, undecoded.
_GEFS_FORECAST = "noaa-gefs-forecast-35-day"
# catalog name -> (store, store variable, units); GEFS tmp2m is stamped "C", which
# pint reads as coulomb.
_GEFS_HINDCAST = {
    "precipitation_surface": (
        "gs://sheerwater-public-datalake/hindcasts/gefs_precip.zarr",
        "precip",
        "mm day-1",
    ),
    "temperature_2m": (
        "sheerwater-datalake/caches/gefs_chc_raw/global1_0_tmp2m.zarr",
        "tmp2m",
        "degree_Celsius",
    ),
}
_GEFS_AUTH_MSG = (
    "GEFS temperature_2m hindcasts need gs://sheerwater-datalake/caches/gefs_chc_raw "
    "(private GCS). Set NEURAL_GCM_SERVICE_CREDENTIALS to the full service-account key "
    "JSON, GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON file path, or run "
    "`gcloud auth application-default login`."
)


def _open_gefs_hindcast(name: str):
    """Open one GEFS hindcast store with init_time decoded to datetime64."""
    import json
    import os

    import numpy as np
    import xarray as xr

    path, source, _ = _GEFS_HINDCAST[name]
    if name == "precipitation_surface":
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


def _hpa_by_prefix(data_vars) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in data_vars:
        match = _HPA_RE.match(name)
        if match:
            groups.setdefault(match.group(1), []).append(name)
    return groups


def _resolve_variables(requested, data_vars, dataset: str):
    """Map `-v` tokens onto catalog names.

    Catalog-exact names pass through. A prefix (`temperature`) or short alias
    (`t`, `gh`) expands to every `*_Nhpa` field of that prefix — not to
    height-above-ground companions like `temperature_2m`. When the dataset
    stores a single `temperature` field (46-day `pressure_level` group),
    `-v t` selects that field. Common precip nicknames
    (`precip`, `precipitation`, `tp`, `total_precipitation`) map to
    `precipitation_surface` when that field exists. `t2m` / `d2m` map to
    the instant `*_2m` field, else the 46-day daily mean (`average_*_2m`); `sst` to
    `sea_surface_temperature`; `msl` / `sp` to the sea-level / surface pressure;
    `u10` / `v10` / `u100` / `v100` to `wind_{u,v}_{10,100}m`.
    """
    if not requested:
        return None
    name_set = set(data_vars)
    hpa = _hpa_by_prefix(data_vars)
    resolved: list[str] = []
    seen: set[str] = set()
    missing: list[str] = []

    def _take(name: str) -> None:
        if name not in seen:
            resolved.append(name)
            seen.add(name)

    for token in requested:
        prefix = _HPA_ALIASES.get(token, token)
        if token in name_set:
            _take(token)
            continue
        kids = hpa.get(prefix)
        if kids:
            for kid in sorted(kids, key=lambda n: -int(_HPA_RE.match(n).group(2))):
                _take(kid)
            continue
        if prefix in name_set:
            _take(prefix)
            continue
        aliased = _PRECIP_ALIASES.get(token) or _SURFACE_ALIASES.get(token)
        # `t2m` / `d2m` are the instant `temperature_2m` / `dew_point_temperature_2m`
        # where a store has them (GFS, GEFS, IFS 15-day); else the 46-day daily mean.
        if aliased and aliased.removeprefix("average_") in name_set:
            aliased = aliased.removeprefix("average_")
        if aliased in name_set:
            _take(aliased)
            continue
        surface = f"{token}_surface"
        if surface in name_set:
            _take(surface)
            continue
        missing.append(token)
    if missing:
        raise UsageError(
            f"variable(s) not in {dataset}: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(data_vars))}"
        )
    return resolved


def _stack_pressure_levels(ds):
    """Combine `*_Nhpa` variables into one field with a `vertical` (hPa) dim."""
    import xarray as xr

    groups: dict[str, list[tuple[int, str]]] = {}
    for name in ds.data_vars:
        match = _HPA_RE.match(name)
        if match:
            groups.setdefault(match.group(1), []).append((int(match.group(2)), name))
    if not groups:
        return ds

    drop = [name for items in groups.values() for _, name in items]
    pieces = [ds.drop_vars(drop)]
    for prefix, items in groups.items():
        items = sorted(items, key=lambda it: -it[0])
        stacked = xr.concat(
            [ds[name].expand_dims(vertical=[float(hpa)]) for hpa, name in items],
            dim="vertical",
        )
        stacked.name = prefix
        stacked["vertical"].attrs.update(
            units="hPa",
            standard_name="air_pressure",
            long_name="pressure",
            positive="down",
            axis="Z",
        )
        pieces.append(stacked.to_dataset())
    return xr.merge(pieces)


def _promote_pressure_dim(ds):
    if "pressure_level" not in ds.dims:
        return ds
    ds = ds.rename({"pressure_level": "vertical"})
    ds["vertical"].attrs.update(
        units="hPa",
        standard_name="air_pressure",
        long_name="pressure",
        positive="down",
        axis="Z",
    )
    return ds


def _merge_pressure_if_needed(ds, dataset, requested):
    """Open `group=pressure_level` when `-v` tokens are not on the root store."""
    if not requested:
        return ds
    try:
        _resolve_variables(requested, ds.data_vars, dataset)
        return ds
    except UsageError:
        pass
    import dynamical_catalog
    import xarray as xr

    try:
        pds = dynamical_catalog.open(dataset, group=_PRESSURE_GROUP, chunks=None)
    except Exception:
        return ds
    return xr.merge([ds, _promote_pressure_dim(pds)], compat="override")


_STAGING_CATALOG = "https://stac-staging.dynamical.org/catalog.json"


def _open_dataset(state, dataset) -> dict:
    """Validate the dataset id, open it, and detect its shape, at most once per run."""
    if "ds" not in state:
        import os

        import dynamical_catalog

        catalog = dynamical_catalog.list()
        if dataset not in catalog:
            os.environ["DYNAMICAL_STAC_CATALOG_URL"] = _STAGING_CATALOG
            dynamical_catalog.clear_cache()
            catalog = dynamical_catalog.list()
        if dataset not in catalog:
            raise UsageError(
                f"unknown dataset {dataset!r}. Available datasets:\n  " + "\n  ".join(catalog)
            )
        ds = dynamical_catalog.open(dataset)

        if "latitude" not in ds.dims or "longitude" not in ds.dims:
            raise UsageError(
                f"{dataset} is on a projected grid (dims {tuple(ds.dims)}); this "
                "fetcher only handles regular 1-D latitude/longitude grids."
            )

        if "ensemble_member" in ds.dims:
            shape = "ensemble"
        elif "lead_time" in ds.dims:
            shape = "forecast"
        elif "time" in ds.dims:
            shape = "analysis"
        else:
            raise UsageError(
                f"{dataset} has an unrecognized shape (dims {tuple(ds.dims)}); "
                "expected an ensemble/deterministic forecast (lead_time) or an analysis (time)."
            )
        state["ds"] = ds
        state["shape"] = shape
    return state


@weather_skill(
    name="dynamical-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--date")
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dataset",
    required=True,
    help="Catalog dataset id (validated against dynamical_catalog.list()).",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
@weather_skill.argument(
    "--max-lead",
    help=(
        "Forecasts only: keep leads up to this one (pandas timedelta: 240h, 10D; "
        "0h keeps only the initial state of instant fields). "
        "Only those leads are downloaded. Default: every lead."
    ),
)
@weather_skill.argument(
    "--hindcast",
    action="store_true",
    help=(
        f"{_GEFS_FORECAST} only: fetch GEFS hindcasts (Wednesday inits 1998-12-30 to "
        "2019-08-28) from Sheerwater's stores, inits in --start-time..--end-time."
    ),
)
def fetch(
    bbox, dataset, date, start_time, end_time, variable, probe_latest, max_lead, hindcast, **kwargs
):
    """Fetch a forecast or analysis from the dynamical.org catalog as a weather-skills Zarr."""
    if hindcast:
        import numpy as np
        import xarray as xr

        dsid = (probe_latest or dataset) if probe_latest is not None else dataset
        if dsid != _GEFS_FORECAST:
            raise UsageError(
                f"--hindcast is only for {_GEFS_FORECAST} (GEFS); {dsid!r} has no hindcasts here."
            )
        if probe_latest is not None:
            latest = _open_gefs_hindcast("precipitation_surface")["init_time"].values.max()
            print(np_to_date(latest).isoformat())
            return
        if date is not None or max_lead is not None:
            raise UsageError(
                "--hindcast takes an init window: use --start-time/--end-time, "
                "not --date or --max-lead."
            )
        if start_time is None or end_time is None:
            raise UsageError("--hindcast needs --start-time and --end-time (init dates).")
        names = _resolve_variables(
            variable or ["precipitation_surface"], list(_GEFS_HINDCAST), f"{dsid} hindcasts"
        )
        start = np.datetime64(start_time.isoformat())
        end = np.datetime64(end_time.isoformat()) + np.timedelta64(1, "D")
        parts = []
        for name in names:
            _, source, units = _GEFS_HINDCAST[name]
            hc = _open_gefs_hindcast(name)
            inits = hc["init_time"].values
            keep = np.nonzero((inits >= start) & (inits < end))[0]
            if keep.size == 0:
                raise DataError(
                    f"no GEFS {name} hindcast init in {start_time}..{end_time} (store runs "
                    f"{np_to_date(inits.min())} to {np_to_date(inits.max())})."
                )
            part = hc[[source]].isel(init_time=keep).rename({source: name})
            part[name].attrs = {"units": units}
            parts.append(part)
        # The precip and tmp2m stores skip different weeks; keep every init and
        # leave a variable NaN where its store has no run. Never filled: say which.
        # Only init_time may differ: the grids and members must match exactly.
        inits = np.unique(np.concatenate([part["init_time"].values for part in parts]))
        try:
            ds = xr.merge([part.reindex(init_time=inits) for part in parts], join="exact")
        except ValueError as exc:
            raise DataError(
                f"GEFS hindcast stores for {', '.join(names)} are on different grids ({exc}); "
                "fetch them in separate calls."
            ) from None
        for part, name in zip(parts, names, strict=True):
            missing = np.setdiff1d(ds["init_time"].values, part["init_time"].values)
            if missing.size:
                print(
                    f"WARNING: GEFS {name} hindcast has no run for {missing.size} init(s), "
                    "left NaN: " + ", ".join(str(np_to_date(t)) for t in missing),
                    file=sys.stderr,
                )
        ds = ds.rename(
            {
                "init_time": "time",
                "member": "number",
                "prediction_timedelta": "step",
                "lat": "latitude",
                "lon": "longitude",
            }
        )
        # Daily leads: step d covers [init + d, init + d + 1 day).
        ds = ds.assign_coords(
            number=ds["number"].values.astype("int64"),
            step=ds["step"].values.astype("int64").astype("timedelta64[D]").astype("m8[ns]"),
        )
        if bbox is not None:
            ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
        else:
            ds = ensure_normalized_longitude(ds, lon_dim="longitude")
        ds = ds.transpose("time", "number", "step", "latitude", "longitude").load()
        print(
            f"Fetched {ds.sizes['time']} GEFS hindcast inits "
            f"{np_to_date(ds['time'].values[0])}..{np_to_date(ds['time'].values[-1])}",
            file=sys.stderr,
        )
        ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
        if "temperature_2m" in ds:
            ds["temperature_2m"].attrs["standard_name"] = "air_temperature"
        ds.attrs.update(
            weather_skills_source="sheerwater:"
            + "+".join(
                "hindcasts/gefs_precip" if n == "precipitation_surface" else "gefs_chc_raw/tmp2m"
                for n in names
            ),
            Conventions="CF-1.13",
        )
        stamp_cf_attrs(ds)
        ds = to_standard_units(ds, variables=names)
        return stamp_data_interval(ds, period="1 day")

    if probe_latest is not None:
        import numpy as np

        dsid = probe_latest or dataset
        if not dsid:
            raise UsageError("--dataset is required (or pass it as --probe-latest <id>).")
        state = _open_dataset({}, dsid)
        is_fc = state["shape"] in ("ensemble", "forecast")
        latest = np.max(state["ds"]["init_time" if is_fc else "time"].values)
        if is_fc:
            # Read one value: the last lead of the newest init at one grid
            # point. NaN there means the long leads are still publishing.
            var = state["ds"][next(iter(state["ds"].data_vars))].sel(init_time=latest)
            point = var.isel({d: -1 if d == "lead_time" else 0 for d in var.dims})
            if np.isnan(float(point.values)):
                print(
                    f"Note: the {np_to_date(latest).isoformat()} init is still publishing; "
                    "its long leads are all-NaN for now.",
                    file=sys.stderr,
                )
        elif latest != latest.astype("datetime64[D]"):
            print(
                f"Note: latest timestamp is {str(latest)[:16]}; that day is partial.",
                file=sys.stderr,
            )
        print(np_to_date(latest).isoformat())
        return

    import numpy as np

    state = _open_dataset({}, dataset)
    ds = state["ds"]
    shape = state["shape"]
    is_forecast = shape in ("ensemble", "forecast")
    if variable:
        ds = _merge_pressure_if_needed(ds, dataset, variable)

    if is_forecast:
        if date is None:
            raise UsageError(f"{dataset} is a forecast dataset; --date is required.")
        if start_time is not None or end_time is not None:
            raise UsageError(
                f"{dataset} is a forecast dataset; use --date, not --start-time/--end-time."
            )
        date_iso = date.isoformat()
    else:
        if max_lead is not None:
            raise UsageError(f"{dataset} is an analysis dataset; --max-lead is for forecasts.")
        if start_time is None or end_time is None:
            raise UsageError(
                f"{dataset} is an analysis dataset; --start-time and --end-time are required."
            )
        if date is not None:
            raise UsageError(
                f"{dataset} is an analysis dataset; use --start-time/--end-time, not --date."
            )
        start_iso = start_time.isoformat()
        end_iso = end_time.isoformat()

    if variable:
        ds = ds[_resolve_variables(variable, ds.data_vars, dataset)]

    if is_forecast:
        inits = ds["init_time"].values
        init_target = np.datetime64(f"{date_iso}T00:00:00").astype(inits.dtype)

        def _no_init() -> DataError:
            return DataError(
                f"{dataset} has no {date_iso} 00 UTC init; available init range is "
                f"{np_to_date(inits.min()).isoformat()}..{np_to_date(inits.max()).isoformat()}."
            )

        if max_lead is not None:
            import pandas as pd

            try:
                limit = pd.Timedelta(max_lead).to_timedelta64()
            except ValueError:
                raise UsageError(
                    f"--max-lead {max_lead!r} is not a timedelta (240h, 10D)"
                ) from None
            # 0h keeps only lead 0 (the initial state of instant fields).
            if limit < np.timedelta64(0, "ns"):
                raise UsageError("--max-lead must not be negative")
            ds = ds.sel(lead_time=slice(None, limit))
        if init_target not in inits:
            raise _no_init()
        try:
            ds = ds.sel(init_time=init_target)
        except KeyError:
            raise _no_init() from None
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])
        rename = {"lead_time": "step"}
        if shape == "ensemble":
            rename["ensemble_member"] = "number"
        ds = ds.rename(rename)
        ds = ds.assign_coords(time=ds["init_time"]).drop_vars("init_time")
    else:
        # --end-time is an inclusive calendar day: keep every sub-daily step of
        # that day (IMERG half-hourly), not just its 00:00 instant.
        day_end = np.datetime64(end_iso) + np.timedelta64(1, "D") - np.timedelta64(1, "ns")
        native = ds["time"].values[:2]
        spacing = native[1] - native[0] if len(native) > 1 else np.timedelta64(1, "D")
        ds = ds.sel(time=slice(np.datetime64(start_iso), day_end))
        if ds.sizes.get("time", 0) == 0:
            raise DataError(f"{dataset} has no data in {start_iso}..{end_iso}.")
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])
        # The catalog only lists published times, so a window running past the
        # latest (or before the first) time comes back shorter. Say so; never pad.
        times = ds["time"].values
        if times[0] > np.datetime64(start_iso) or times[-1] + spacing <= day_end:
            print(
                f"WARNING: {dataset} covers only {str(times[0])[:16]}..{str(times[-1])[:16]} "
                f"of the requested {start_iso}..{end_iso}; writing that partial window "
                "(not padded).",
                file=sys.stderr,
            )

    # Spatial subset after the time/variable selection: an antimeridian bbox
    # concatenates two longitude wings, which reads every selected value.
    if bbox is not None:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
    else:
        ds = ensure_normalized_longitude(ds, lon_dim="longitude")

    # Catalog averaged fields (precip, 46-day daily means) are labelled at the END
    # of their interval, so lead 0 is all-NaN. When every selected field is NaN
    # there (same rule as ecmwf-fetch), drop lead 0 and relabel each remaining
    # step to the previous step value: step = interval start. Using the previous
    # value (not a fixed spacing) stays correct for GEFS 3 h -> 6 h leads.
    # Instant fields (t2m on GEFS, winds, pressure levels) keep lead 0.
    if is_forecast and len(ds.data_vars) > 0:
        # Read the subset once: the NaN checks below would otherwise re-read it.
        ds = ds.load()
        # Only lead 0 kept (--max-lead below the first interval's end): interval
        # fields have no value there. Drop them and keep the instant fields.
        if ds.sizes["step"] == 1 and ds["step"].values[0] == np.timedelta64(0, "ns"):
            no_lead0 = [name for name in ds.data_vars if bool(ds[name].isnull().all())]
            if len(no_lead0) == len(ds.data_vars):
                raise UsageError(
                    f"{', '.join(no_lead0)} has no value at lead 0: {dataset} labels "
                    "averaged and accumulated fields at the end of their interval, and "
                    "--max-lead kept only lead 0. Pass a --max-lead that reaches past the "
                    "first lead (e.g. 3h or 6h)."
                )
            if no_lead0:
                print(
                    f"WARNING: dropped {', '.join(no_lead0)}: no value at lead 0 ({dataset} "
                    "labels averaged and accumulated fields at the end of their interval). "
                    "Pass a --max-lead that reaches past the first lead to keep them.",
                    file=sys.stderr,
                )
                ds = ds.drop_vars(no_lead0)
        empty = np.ones(ds.sizes["step"], dtype=bool)
        for name in ds.data_vars:
            other = [d for d in ds[name].dims if d != "step"]
            empty &= ds[name].isnull().all(other).values
        if empty.all():
            raise DataError(
                f"{dataset} {date_iso} init has no data for the requested variables and "
                "bbox (still publishing?); try the previous day's init."
            )
        # A fresh init is still publishing its long leads (all-NaN). Drop that
        # tail and say so instead of writing empty leads.
        last = int(np.nonzero(~empty)[0][-1])
        if last < ds.sizes["step"] - 1:
            hours = ds["step"].values / np.timedelta64(1, "h")
            print(
                f"WARNING: {dataset} {date_iso} init has no data after catalog lead "
                f"{hours[last]:g} h (of {hours[-1]:g} h; still publishing?). Dropped the "
                "empty leads (not padded); use the previous day's init for every lead.",
                file=sys.stderr,
            )
            ds = ds.isel(step=slice(0, last + 1))
    if is_forecast and ds.sizes.get("step", 0) > 1 and len(ds.data_vars) > 0:
        first = ds.isel(step=0)
        if all(bool(first[name].isnull().all()) for name in ds.data_vars):
            leads = ds["step"].values
            ds = ds.isel(step=slice(1, None)).assign_coords(step=leads[:-1])
            # GEFS / IFS ENS go 3- to 6-hourly. Write each cell's [start, end]
            # here: stamp_data_interval would infer bounds as if steps were ends.
            if len(set(np.diff(leads))) > 1:
                pairs = np.stack([leads[:-1], leads[1:]], axis=1)
                ds = ds.assign_coords(step_bounds=(("step", "nv"), pairs))
                ds["step"].attrs["bounds"] = "step_bounds"
            for name in ds.data_vars:
                note = (
                    "dynamical-fetch relabelled steps to interval start: "
                    "step s covers [s, next lead)."
                )
                ds[name].attrs["comment"] = f"{ds[name].attrs.get('comment', '')} {note}".strip()
            print(
                "Dropped all-NaN lead 0 and relabelled steps to interval start.",
                file=sys.stderr,
            )
    ds = _stack_pressure_levels(ds)

    print(f"Fetching dynamical:{dataset} (shape={shape})", file=sys.stderr)

    ds.attrs.update(
        weather_skills_source=f"dynamical:{dataset}",
        Conventions="CF-1.13",
    )
    stamp_cf_attrs(ds)
    # Catalog datasets mix precip with dimensionless companions (e.g. IMERG
    # precipitation_quality_index_surface, units "1"). Convert each variable
    # independently so one inconvertible field does not abort the fetch.
    out = ds
    for name in list(ds.data_vars):
        try:
            out = to_standard_units(out, variables=[name])
        except UsageError:
            continue
    return stamp_data_interval(out)


if __name__ == "__main__":
    fetch()

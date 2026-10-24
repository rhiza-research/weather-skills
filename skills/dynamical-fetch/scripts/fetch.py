# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "dynamical-catalog==1.0.1",
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
_SKILL_VERSION = "0.0.5"

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
    help=("Forecasts only: keep output steps up to this one (240h, 10D). Default: every step."),
)
def fetch(bbox, dataset, date, start_time, end_time, variable, probe_latest, max_lead, **kwargs):
    """Fetch a forecast or analysis from the dynamical.org catalog as a weather-skills Zarr."""
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
            if limit < np.timedelta64(0, "ns"):
                raise UsageError("--max-lead must not be negative")
            # One catalog lead past the limit: end-labelled fields move back one
            # step below, and the extra lead is trimmed after that.
            n = int((ds["lead_time"].values <= limit).sum())
            ds = ds.isel(lead_time=slice(0, n + 1))
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
    if is_forecast and max_lead is not None:
        ds = ds.sel(step=slice(None, limit))
        # Mixed with instant fields, an end-labelled field has no value at step 0.
        empty = [name for name in ds.data_vars if bool(ds[name].isnull().all())]
        if empty:
            print(
                f"WARNING: dropped {', '.join(empty)}: no value up to --max-lead {max_lead} "
                "(averaged fields are labelled at the end of their interval).",
                file=sys.stderr,
            )
            ds = ds.drop_vars(empty)
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

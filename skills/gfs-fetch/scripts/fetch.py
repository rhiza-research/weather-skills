# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
#   "dynamical-catalog==1.0.1",
#   "pandas",
# ]
# ///
"""Fetch a NOAA GFS or GEFS forecast or analysis from the dynamical.org catalog as a weather-skills Zarr."""

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
_SKILL_VERSION = "0.0.1"

# --product -> dynamical.org catalog id.
_PRODUCTS = {
    "gfs": "noaa-gfs-forecast",
    "gefs": "noaa-gefs-forecast-35-day",
    "gfs-analysis": "noaa-gfs-analysis",
    "gefs-analysis": "noaa-gefs-analysis",
}

# Forecast bookkeeping / CRS scalar coords dropped from the standard output.
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

# Catalog stores selected pressure-level fields as separate 2-D variables
# (`geopotential_height_500hpa`). Stack those onto the ontology `vertical` dim.
_HPA_RE = re.compile(r"^(.+)_(\d+)hpa$")
_HPA_ALIASES = {"gh": "geopotential_height"}
# Other fetchers (TAHMO, ARCO, S2S) use these; the catalog stores surface precip as
# precipitation_surface.
_PRECIP_ALIASES = {
    "precip": "precipitation_surface",
    "precipitation": "precipitation_surface",
    "tp": "precipitation_surface",
    "total_precipitation": "precipitation_surface",
    "pr": "precipitation_surface",
}
_SURFACE_ALIASES = {
    "t2m": "temperature_2m",
    "2m_temperature": "temperature_2m",
    "2_m_temperature": "temperature_2m",
    "msl": "pressure_reduced_to_mean_sea_level",
    "sp": "pressure_surface",
    "u10": "wind_u_10m",
    "v10": "wind_v_10m",
    "u100": "wind_u_100m",
    "v100": "wind_v_100m",
}


def _hpa_by_prefix(data_vars) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in data_vars:
        match = _HPA_RE.match(name)
        if match:
            groups.setdefault(match.group(1), []).append(name)
    return groups


def _resolve_variables(requested, data_vars, dataset: str):
    """Map `-v` tokens onto catalog names.

    Catalog-exact names pass through. A prefix (`geopotential_height`) or short
    alias (`gh`) expands to every `*_Nhpa` field of that prefix. Common precip
    nicknames (`precip`, `precipitation`, `tp`, `total_precipitation`, `pr`) map
    to `precipitation_surface`. `t2m` maps to `temperature_2m`; `msl` / `sp` to
    the sea-level / surface pressure; `u10` / `v10` / `u100` / `v100` to
    `wind_{u,v}_{10,100}m`. `NAME` also matches `NAME_surface`.
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
        aliased = _PRECIP_ALIASES.get(token) or _SURFACE_ALIASES.get(token)
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
            f"Available: {', '.join(sorted(data_vars))}\n"
            "Run with --list-variables to see them with units."
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


def _open_dataset(dataset) -> dict:
    """Open the catalog dataset and detect its shape from its dims."""
    import dynamical_catalog

    ds = dynamical_catalog.open(dataset)
    if "ensemble_member" in ds.dims:
        shape = "ensemble"
    elif "lead_time" in ds.dims:
        shape = "forecast"
    else:
        shape = "analysis"
    return {"ds": ds, "shape": shape}


@weather_skill(
    name="gfs-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--product",
    choices=tuple(_PRODUCTS),
    default="gefs",
    help="gfs, gefs (35-day ensemble), gfs-analysis or gefs-analysis. Default: gefs.",
)
@weather_skill.argument("--date")
@weather_skill.argument(
    "--start-time",
    action="append",
    help="Window start; repeat with --end-time for several windows (each January).",
)
@weather_skill.argument("--end-time", action="append", help="Window end, paired with --start-time.")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--probe-latest",
    action="store_true",
    probe=True,
    help=(
        "Print the --product's latest init or analysis day (YYYY-MM-DD) on stdout and exit. "
        "Does not download fields."
    ),
)
@weather_skill.argument(
    "--list-variables",
    action="store_true",
    probe=True,
    help=(
        "Print the --product's variables (name, units, long name) on stdout and exit. "
        "Does not download fields."
    ),
)
@weather_skill.argument(
    "--max-lead",
    help=("Forecasts only: keep output steps up to this one (240h, 10D). Default: every step."),
)
def fetch(
    bbox,
    product,
    date,
    start_time,
    end_time,
    variable,
    probe_latest,
    list_variables,
    max_lead,
    **kwargs,
):
    """Fetch a NOAA GFS or GEFS forecast or analysis from the dynamical.org catalog."""
    dataset = _PRODUCTS[product]
    if list_variables:
        ds = _open_dataset(dataset)["ds"]
        print(f"{product} ({dataset}) variables (pass the name to -v):")
        for name in sorted(ds.data_vars):
            attrs = ds[name].attrs
            print(f"  {name}\t{attrs.get('units', '')}\t{attrs.get('long_name', '')}")
        return
    if probe_latest:
        import numpy as np

        state = _open_dataset(dataset)
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

    state = _open_dataset(dataset)
    ds = state["ds"]
    shape = state["shape"]
    is_forecast = shape in ("ensemble", "forecast")

    # One (start, end) pair per window; --end-time is an inclusive calendar day.
    windows = list(zip(start_time or [], end_time or [], strict=True))
    if is_forecast:
        if (date is None) == (not windows):
            raise UsageError(
                f"{product} is a forecast; pass --date for one init, or "
                "--start-time/--end-time for a range of inits (not both)."
            )
        when = date.isoformat() if date is not None else "selected"
    else:
        if max_lead is not None:
            raise UsageError(f"{product} is an analysis; --max-lead is for forecasts.")
        if not windows:
            raise UsageError(f"{product} is an analysis; --start-time and --end-time are required.")
        if date is not None:
            raise UsageError(f"{product} is an analysis; use --start-time/--end-time, not --date.")
    spans = [
        (np.datetime64(start.isoformat()), np.datetime64(end.isoformat()) + np.timedelta64(1, "D"))
        for start, end in windows
    ]
    window_text = ", ".join(f"{start}..{end}" for start, end in windows)

    if variable:
        ds = ds[_resolve_variables(variable, ds.data_vars, dataset)]
    # Every fetcher writes rainfall as `precip`.
    if "precipitation_surface" in ds:
        ds = ds.rename({"precipitation_surface": "precip"})

    if is_forecast:
        inits = ds["init_time"].values
        init_range = f"{np_to_date(inits.min()).isoformat()}..{np_to_date(inits.max()).isoformat()}"

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
        if date is not None:
            init_target = np.datetime64(f"{date.isoformat()}T00:00:00").astype(inits.dtype)
            if init_target not in inits:
                raise DataError(
                    f"{dataset} has no {date.isoformat()} 00 UTC init; "
                    f"available init range is {init_range}."
                )
            ds = ds.sel(init_time=init_target)
        else:
            # 00 UTC inits only, as with --date: the windows select init times.
            midnight = inits == inits.astype("datetime64[D]")
            keep = np.zeros(inits.size, dtype=bool)
            for lo, hi in spans:
                keep |= midnight & (inits >= lo) & (inits < hi)
            if not keep.any():
                raise DataError(
                    f"{dataset} has no 00 UTC init in {window_text}; "
                    f"available init range is {init_range}."
                )
            ds = ds.isel(init_time=np.nonzero(keep)[0])
            print(f"Selected {int(keep.sum())} inits.", file=sys.stderr)
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])
        rename = {"lead_time": "step"}
        if shape == "ensemble":
            rename["ensemble_member"] = "number"
        ds = ds.rename(rename)
        # One init: scalar `time`. A range of inits: a `time` axis (the init).
        ds = ds.rename({"init_time": "time"})
    else:
        # Each window keeps every sub-daily step of its end day, not just its
        # 00:00 instant. Windows may overlap.
        times = ds["time"].values
        spacing = times[1] - times[0] if len(times) > 1 else np.timedelta64(1, "D")
        keep = np.zeros(times.size, dtype=bool)
        for (start, end), (lo, hi) in zip(windows, spans, strict=True):
            inside = (times >= lo) & (times < hi)
            keep |= inside
            # The catalog only lists published times, so a window running past
            # the latest (or before the first) time comes back shorter. Say so;
            # never pad.
            if not inside.any():
                print(f"WARNING: {dataset} has no data in {start}..{end}.", file=sys.stderr)
            elif times[inside][0] > lo or times[inside][-1] + spacing < hi:
                print(
                    f"WARNING: {dataset} covers only {str(times[inside][0])[:16]}.."
                    f"{str(times[inside][-1])[:16]} of the requested {start}..{end}; "
                    "writing that partial window (not padded).",
                    file=sys.stderr,
                )
        if not keep.any():
            raise DataError(f"{dataset} has no data in {window_text}.")
        ds = ds.isel(time=np.nonzero(keep)[0])
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])

    # Spatial subset after the time/variable selection: an antimeridian bbox
    # concatenates two longitude wings, which reads every selected value.
    if bbox is not None:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
    else:
        ds = ensure_normalized_longitude(ds, lon_dim="longitude")

    # Catalog averaged fields (precip, radiation) are labelled at the END of their
    # interval, so lead 0 is all-NaN. When every selected field is NaN there
    # (same rule as ecmwf-fetch), drop lead 0 and relabel each remaining step to
    # the previous step value: step = interval start. Using the previous value
    # (not a fixed spacing) stays correct for 1 h -> 3 h and 3 h -> 6 h leads.
    # Instant fields (t2m, winds, 500 hPa height) keep lead 0.
    if is_forecast and len(ds.data_vars) > 0:
        # Read the subset once: the NaN checks below would otherwise re-read it.
        ds = ds.load()
        # GEFS stores members before leads; put every forecast in one order.
        # Only after the load: without dask, a lazy transpose makes the read
        # take gigabytes for a small bbox.
        ds = ds.transpose(*[d for d in ("time", "step", "number") if d in ds.dims], ...)
        empty = np.ones(ds.sizes["step"], dtype=bool)
        for name in ds.data_vars:
            other = [d for d in ds[name].dims if d != "step"]
            empty &= ds[name].isnull().all(other).values
        if empty.all():
            raise DataError(
                f"{dataset} {when} init has no data for the requested variables and "
                "bbox (still publishing?); try the previous day's init."
            )
        # A fresh init is still publishing its long leads (all-NaN). Drop that
        # tail and say so instead of writing empty leads.
        last = int(np.nonzero(~empty)[0][-1])
        if last < ds.sizes["step"] - 1:
            hours = ds["step"].values / np.timedelta64(1, "h")
            print(
                f"WARNING: {dataset} {when} init has no data after catalog lead "
                f"{hours[last]:g} h (of {hours[-1]:g} h; still publishing?). Dropped the "
                "empty leads (not padded); use the previous day's init for every lead.",
                file=sys.stderr,
            )
            ds = ds.isel(step=slice(0, last + 1))
        # A range of inits: the newest may still be publishing its long leads
        # while older inits fill them, so the check above passes. Its empty
        # leads stay NaN (not filled); say which init is short.
        if "time" in ds.dims:
            init_empty = np.ones((ds.sizes["time"], ds.sizes["step"]), dtype=bool)
            for name in ds.data_vars:
                other = [d for d in ds[name].dims if d not in ("time", "step")]
                init_empty &= ds[name].isnull().all(other).transpose("time", "step").values
            hours = ds["step"].values / np.timedelta64(1, "h")
            for i, row in enumerate(init_empty):
                init = str(ds["time"].values[i])[:13]
                if row.all():
                    print(f"WARNING: {dataset} {init} UTC init has no data (NaN).", file=sys.stderr)
                elif row[-1]:
                    print(
                        f"WARNING: {dataset} {init} UTC init has no data after catalog lead "
                        f"{hours[np.nonzero(~row)[0][-1]]:g} h (still publishing?); its later "
                        "leads are NaN (not filled).",
                        file=sys.stderr,
                    )
    if is_forecast and ds.sizes.get("step", 0) > 1 and len(ds.data_vars) > 0:
        first = ds.isel(step=0)
        if all(bool(first[name].isnull().all()) for name in ds.data_vars):
            leads = ds["step"].values
            ds = ds.isel(step=slice(1, None)).assign_coords(step=leads[:-1])
            # GFS / GEFS change lead spacing. Write each cell's [start, end]
            # here: stamp_data_interval would infer bounds as if steps were ends.
            if len(set(np.diff(leads))) > 1:
                pairs = np.stack([leads[:-1], leads[1:]], axis=1)
                ds = ds.assign_coords(step_bounds=(("step", "nv"), pairs))
                ds["step"].attrs["bounds"] = "step_bounds"
            for name in ds.data_vars:
                note = "gfs-fetch relabelled steps to interval start: step s covers [s, next lead)."
                ds[name].attrs["comment"] = f"{ds[name].attrs.get('comment', '')} {note}".strip()
            print(
                "Dropped all-NaN lead 0 and relabelled steps to interval start.",
                file=sys.stderr,
            )
        elif not any(bool(first[name].isnull().all()) for name in ds.data_vars):
            # Only instant fields: they keep the catalog labels. On uneven leads
            # give each sample the cell [s, next lead) like the relabelled
            # averages above, so a day bin holds the samples labelled in that
            # day; stamp_data_interval would infer cells ending at each step.
            leads = ds["step"].values
            if len(set(np.diff(leads))) > 1:
                ends = np.append(leads[1:], leads[-1] + (leads[-1] - leads[-2]))
                pairs = np.stack([leads, ends], axis=1)
                ds = ds.assign_coords(step_bounds=(("step", "nv"), pairs))
                ds["step"].attrs["bounds"] = "step_bounds"
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
    # The stores mix precip and temperature with dimensionless fields (categorical
    # rain, units "1"). Convert each variable independently so one inconvertible
    # field does not abort the fetch.
    out = ds
    for name in list(ds.data_vars):
        try:
            out = to_standard_units(out, variables=[name])
        except UsageError:
            continue
    # Stamp the lead spacing: with an init window, `time` is the init axis and
    # its 1-day gaps are not the cell length.
    return stamp_data_interval(out, dim="step" if is_forecast else None)


if __name__ == "__main__":
    fetch()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "dynamical-catalog==1.0.1",
#   "pandas",
# ]
# ///
"""Fetch a DWD ICON-EU forecast from the dynamical.org catalog as a weather-skills Zarr."""

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

_DATASET = "dwd-icon-eu-forecast-5-day"

# Forecast bookkeeping / CRS scalar coords dropped from the standard output.
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

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
    "d2m": "dew_point_temperature_2m",
    "msl": "pressure_reduced_to_mean_sea_level",
    "sp": "pressure_surface",
    "u10": "wind_u_10m",
    "v10": "wind_v_10m",
}


def _resolve_variables(requested, data_vars):
    """Map `-v` tokens onto catalog names.

    Catalog-exact names pass through. Common precip nicknames (`precip`,
    `precipitation`, `tp`, `total_precipitation`, `pr`) map to
    `precipitation_surface`. `t2m` / `d2m` map to `temperature_2m` /
    `dew_point_temperature_2m`; `msl` / `sp` to the sea-level / surface
    pressure; `u10` / `v10` to `wind_{u,v}_10m`. `NAME` also matches
    `NAME_surface`.
    """
    if not requested:
        return None
    name_set = set(data_vars)
    resolved: list[str] = []
    missing: list[str] = []
    for token in requested:
        aliased = _PRECIP_ALIASES.get(token) or _SURFACE_ALIASES.get(token)
        for name in (token, aliased, f"{token}_surface"):
            if name in name_set:
                if name not in resolved:
                    resolved.append(name)
                break
        else:
            missing.append(token)
    if missing:
        raise UsageError(
            f"variable(s) not in {_DATASET}: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(data_vars))}\n"
            "Run with --list-variables to see them with units."
        )
    return resolved


def _open_dataset():
    import dynamical_catalog

    return dynamical_catalog.open(_DATASET)


@weather_skill(
    name="icon-eu-fetch",
    version=_SKILL_VERSION,
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
    help="Print the latest init (YYYY-MM-DD) on stdout and exit. Does not download fields.",
)
@weather_skill.argument(
    "--list-variables",
    action="store_true",
    probe=True,
    help="Print the variables (name, units, long name) on stdout and exit. Does not download fields.",
)
@weather_skill.argument(
    "--max-lead",
    help="Keep output steps up to this one (48h, 3D). Default: every step.",
)
def fetch(
    bbox,
    date,
    start_time,
    end_time,
    variable,
    probe_latest,
    list_variables,
    max_lead,
    **kwargs,
):
    """Fetch a DWD ICON-EU forecast from the dynamical.org catalog."""
    if list_variables:
        ds = _open_dataset()
        print(f"{_DATASET} variables (pass the name to -v):")
        for name in sorted(ds.data_vars):
            attrs = ds[name].attrs
            print(f"  {name}\t{attrs.get('units', '')}\t{attrs.get('long_name', '')}")
        return
    if probe_latest:
        import numpy as np

        ds = _open_dataset()
        latest = np.max(ds["init_time"].values)
        # Read one value: the last lead of the newest init at one grid point.
        # NaN there means the long leads are still publishing.
        var = ds[next(iter(ds.data_vars))].sel(init_time=latest)
        point = var.isel({d: -1 if d == "lead_time" else 0 for d in var.dims})
        if np.isnan(float(point.values)):
            print(
                f"Note: the {np_to_date(latest).isoformat()} init is still publishing; "
                "its long leads are all-NaN for now.",
                file=sys.stderr,
            )
        print(np_to_date(latest).isoformat())
        return

    import numpy as np

    ds = _open_dataset()

    # One (start, end) pair per window; --end-time is an inclusive calendar day.
    windows = list(zip(start_time or [], end_time or [], strict=True))
    if (date is None) == (not windows):
        raise UsageError(
            "pass --date for one init, or --start-time/--end-time for a range of inits (not both)."
        )
    when = date.isoformat() if date is not None else "selected"
    spans = [
        (np.datetime64(start.isoformat()), np.datetime64(end.isoformat()) + np.timedelta64(1, "D"))
        for start, end in windows
    ]
    window_text = ", ".join(f"{start}..{end}" for start, end in windows)

    if variable:
        ds = ds[_resolve_variables(variable, ds.data_vars)]
    # Every fetcher writes rainfall as `precip`.
    if "precipitation_surface" in ds:
        ds = ds.rename({"precipitation_surface": "precip"})

    inits = ds["init_time"].values
    init_range = f"{np_to_date(inits.min()).isoformat()}..{np_to_date(inits.max()).isoformat()}"

    if max_lead is not None:
        import pandas as pd

        try:
            limit = pd.Timedelta(max_lead).to_timedelta64()
        except ValueError:
            raise UsageError(f"--max-lead {max_lead!r} is not a timedelta (48h, 3D)") from None
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
                f"{_DATASET} has no {date.isoformat()} 00 UTC init; "
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
                f"{_DATASET} has no 00 UTC init in {window_text}; "
                f"available init range is {init_range}."
            )
        ds = ds.isel(init_time=np.nonzero(keep)[0])
        print(f"Selected {int(keep.sum())} inits.", file=sys.stderr)
    ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])
    # One init: scalar `time`. A range of inits: a `time` axis (the init).
    ds = ds.rename({"lead_time": "step", "init_time": "time"})

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
    # (not a fixed spacing) stays correct where leads go 1- to 3-hourly.
    # Instant fields (t2m, winds) keep lead 0.
    if len(ds.data_vars) > 0:
        # Read the subset once: the NaN checks below would otherwise re-read it.
        ds = ds.load()
        ds = ds.transpose(*[d for d in ("time", "step") if d in ds.dims], ...)
        empty = np.ones(ds.sizes["step"], dtype=bool)
        for name in ds.data_vars:
            other = [d for d in ds[name].dims if d != "step"]
            empty &= ds[name].isnull().all(other).values
        if empty.all():
            raise DataError(
                f"{_DATASET} {when} init has no data for the requested variables and "
                "bbox (still publishing?); try the previous day's init."
            )
        # A fresh init is still publishing its long leads (all-NaN). Drop that
        # tail and say so instead of writing empty leads.
        last = int(np.nonzero(~empty)[0][-1])
        if last < ds.sizes["step"] - 1:
            hours = ds["step"].values / np.timedelta64(1, "h")
            print(
                f"WARNING: {_DATASET} {when} init has no data after catalog lead "
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
                    print(
                        f"WARNING: {_DATASET} {init} UTC init has no data (NaN).", file=sys.stderr
                    )
                elif row[-1]:
                    print(
                        f"WARNING: {_DATASET} {init} UTC init has no data after catalog lead "
                        f"{hours[np.nonzero(~row)[0][-1]]:g} h (still publishing?); its later "
                        "leads are NaN (not filled).",
                        file=sys.stderr,
                    )
    if ds.sizes.get("step", 0) > 1 and len(ds.data_vars) > 0:
        first = ds.isel(step=0)
        if all(bool(first[name].isnull().all()) for name in ds.data_vars):
            leads = ds["step"].values
            ds = ds.isel(step=slice(1, None)).assign_coords(step=leads[:-1])
            # Leads go 1- to 3-hourly. Write each cell's [start, end] here:
            # stamp_data_interval would infer bounds as if steps were ends.
            if len(set(np.diff(leads))) > 1:
                pairs = np.stack([leads[:-1], leads[1:]], axis=1)
                ds = ds.assign_coords(step_bounds=(("step", "nv"), pairs))
                ds["step"].attrs["bounds"] = "step_bounds"
            for name in ds.data_vars:
                note = "icon-eu-fetch relabelled steps to interval start: step s covers [s, next lead)."
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
    if max_lead is not None:
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

    print(f"Fetching dynamical:{_DATASET}", file=sys.stderr)

    ds.attrs.update(
        weather_skills_source=f"dynamical:{_DATASET}",
        Conventions="CF-1.13",
    )
    stamp_cf_attrs(ds)
    # Convert each variable independently so one inconvertible field (cloud cover
    # in percent, CAPE) does not abort the fetch.
    out = ds
    for name in list(ds.data_vars):
        try:
            out = to_standard_units(out, variables=[name])
        except UsageError:
            continue
    # Stamp the lead spacing: with an init window, `time` is the init axis and
    # its 1-day gaps are not the cell length.
    return stamp_data_interval(out, dim="step")


if __name__ == "__main__":
    fetch()

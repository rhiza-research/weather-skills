# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "dynamical-catalog==1.0.1",
# ]
# ///
"""Fetch NOAA MRMS hourly CONUS precipitation from the dynamical.org catalog as a weather-skills Zarr."""

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

_DATASET = "noaa-mrms-conus-analysis-hourly"

# CRS scalar coord dropped from the standard output.
_DROP_COORDS = ("spatial_ref",)

# Other fetchers (TAHMO, ARCO, S2S) use these; the catalog stores surface precip as
# precipitation_surface.
_PRECIP_ALIASES = {
    "precip": "precipitation_surface",
    "precipitation": "precipitation_surface",
    "tp": "precipitation_surface",
    "total_precipitation": "precipitation_surface",
    "pr": "precipitation_surface",
}


def _resolve_variables(requested, data_vars):
    """Map `-v` tokens onto catalog names.

    Catalog-exact names pass through. Common precip nicknames (`precip`,
    `precipitation`, `tp`, `total_precipitation`, `pr`) map to
    `precipitation_surface`. `NAME` also matches `NAME_surface`
    (`precipitation_radar_only`).
    """
    if not requested:
        return None
    name_set = set(data_vars)
    resolved: list[str] = []
    missing: list[str] = []
    for token in requested:
        for name in (token, _PRECIP_ALIASES.get(token), f"{token}_surface"):
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
    name="mrms-fetch",
    version=_SKILL_VERSION,
)
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
    help="Print the latest day (YYYY-MM-DD) on stdout and exit. Does not download fields.",
)
@weather_skill.argument(
    "--list-variables",
    action="store_true",
    probe=True,
    help="Print the variables (name, units, long name) on stdout and exit. Does not download fields.",
)
def fetch(
    bbox,
    start_time,
    end_time,
    variable,
    probe_latest,
    list_variables,
    **kwargs,
):
    """Fetch NOAA MRMS hourly CONUS precipitation from the dynamical.org catalog."""
    if list_variables:
        ds = _open_dataset()
        print(f"{_DATASET} variables (pass the name to -v):")
        for name in sorted(ds.data_vars):
            attrs = ds[name].attrs
            print(f"  {name}\t{attrs.get('units', '')}\t{attrs.get('long_name', '')}")
        return
    if probe_latest:
        import numpy as np

        latest = np.max(_open_dataset()["time"].values)
        if latest != latest.astype("datetime64[D]"):
            print(
                f"Note: latest timestamp is {str(latest)[:16]}; that day is partial.",
                file=sys.stderr,
            )
        print(np_to_date(latest).isoformat())
        return

    import numpy as np

    # One (start, end) pair per window; --end-time is an inclusive calendar day.
    windows = list(zip(start_time or [], end_time or [], strict=True))
    if not windows:
        raise UsageError("--start-time and --end-time are required.")

    ds = _open_dataset()
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

    # Each window keeps every hour of its end day, not just its 00:00 instant.
    # Windows may overlap.
    times = ds["time"].values
    spacing = times[1] - times[0] if len(times) > 1 else np.timedelta64(1, "D")
    keep = np.zeros(times.size, dtype=bool)
    for (start, end), (lo, hi) in zip(windows, spans, strict=True):
        inside = (times >= lo) & (times < hi)
        keep |= inside
        # The catalog only lists published times, so a window running past the
        # latest (or before the first) time comes back shorter. Say so; never pad.
        if not inside.any():
            print(f"WARNING: {_DATASET} has no data in {start}..{end}.", file=sys.stderr)
        elif times[inside][0] > lo or times[inside][-1] + spacing < hi:
            print(
                f"WARNING: {_DATASET} covers only {str(times[inside][0])[:16]}.."
                f"{str(times[inside][-1])[:16]} of the requested {start}..{end}; "
                "writing that partial window (not padded).",
                file=sys.stderr,
            )
    if not keep.any():
        raise DataError(f"{_DATASET} has no data in {window_text}.")
    ds = ds.isel(time=np.nonzero(keep)[0])
    ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])

    # Spatial subset after the time/variable selection: an antimeridian bbox
    # concatenates two longitude wings, which reads every selected value.
    if bbox is not None:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
    else:
        ds = ensure_normalized_longitude(ds, lon_dim="longitude")

    print(f"Fetching dynamical:{_DATASET}", file=sys.stderr)

    ds.attrs.update(
        weather_skills_source=f"dynamical:{_DATASET}",
        Conventions="CF-1.13",
    )
    stamp_cf_attrs(ds)
    # The store mixes precip with dimensionless fields (precipitation type,
    # units "1"). Convert each variable independently so one inconvertible
    # field does not abort the fetch.
    out = ds
    for name in list(ds.data_vars):
        try:
            out = to_standard_units(out, variables=[name])
        except UsageError:
            continue
    return stamp_data_interval(out)


if __name__ == "__main__":
    fetch()

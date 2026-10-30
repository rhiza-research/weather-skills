# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.2",
# ]
# ///
"""Combine the variables of two or more Zarrs into one dataset on their shared coordinates."""

import re
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import cf_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.5"


@weather_skill(name="merge", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
def merge(ds, **kwargs):
    """Combine the variables of two or more Zarrs into one dataset on their shared coordinates."""
    import pint
    import xarray as xr

    dss = list(ds)
    if len(dss) < 2:
        raise UsageError(f"merge needs at least two inputs (repeat -i), got {len(dss)}")

    owner = {}
    for i, item in enumerate(dss):
        for name in item.data_vars:
            if name in owner:
                raise UsageError(
                    f"variable {name!r} is in input {owner[name] + 1} and input {i + 1}; "
                    "give one a new name first with edit-coords --rename OLD=NEW"
                )
            owner[name] = i

    for i, item in enumerate(dss):
        for dim, index in item.indexes.items():
            if index.has_duplicates:
                raise UsageError(
                    f"input {i + 1} repeats values along {dim!r}; "
                    "keep each value once first (select or aggregate-temporal)"
                )
        # A forecast's single init `time` would be replaced by another input's
        # time axis, losing the init.
        for name, coord in item.coords.items():
            if coord.ndim == 0 and any(name in other.dims for other in dss):
                raise UsageError(
                    f"input {i + 1} has one {name!r} value where another input has a {name!r} "
                    "axis; for a forecast, turn its steps into times with step-to-time first"
                )

    try:
        out = xr.merge(dss, join="inner", compat="no_conflicts", combine_attrs="drop_conflicts")
    except pint.DimensionalityError as exc:
        raise UsageError(f"inputs carry incompatible units on a shared coordinate: {exc}") from None
    except xr.MergeError as exc:
        found = re.search(r"variable '([^']+)'", str(exc))
        name = found.group(1) if found else "a coordinate"
        # Bounds differ when one input labels steps [prev, step] (an instant
        # field such as MSLP) and another [step, next) (an average such as precip).
        if any(c.attrs.get("bounds") == name for item in dss for c in item.coords.values()):
            raise UsageError(
                f"inputs have different {name!r} (e.g. an instant field labelled [prev, step] "
                "and an averaged one labelled [step, next)); they describe different intervals"
            ) from None
        raise UsageError(
            f"inputs have different values of {name!r} (e.g. two forecast init dates, or one "
            "station at two locations); merge only inputs that agree on it"
        ) from None

    latlon = {cf_dim(out, "latitude"), cf_dim(out, "longitude")}
    for dim in out.dims:
        largest = max(item.sizes[dim] for item in dss if dim in item.dims)
        regrid = (
            "put one on the other's grid with coarsen --reference-grid" if dim in latlon else ""
        )
        if out.sizes[dim] == 0:
            raise UsageError(
                f"the inputs share no {dim!r} values" + (f"; {regrid}" if regrid else "")
            )
        if out.sizes[dim] < largest:
            print(
                f"merge: kept the {out.sizes[dim]} of {largest} {dim!r} values every input has"
                + (f"; {regrid} to keep them all" if regrid else ""),
                file=sys.stderr,
            )
    return out


if __name__ == "__main__":
    merge()

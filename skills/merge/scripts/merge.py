# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Combine the variables of several Zarrs into one dataset on their shared coordinates."""

import sys

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import cf_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


@weather_skill(name="merge", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument(
    "--by-year",
    action="store_true",
    help="Match values by calendar year: replace each input's time axis with a year axis first.",
)
def merge(ds, by_year, **kwargs):
    """Combine the variables of several Zarrs into one dataset on their shared coordinates."""
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
                    "give one a new name with the rename skill first"
                )
            owner[name] = i

    if by_year:
        for i, item in enumerate(dss):
            t = cf_dim(item, "time")
            if t is None or t not in item.dims:
                raise UsageError(f"--by-year needs a time dim; input {i + 1} has {list(item.dims)}")
            years = item[t].dt.year.values
            unique, counts = np.unique(years, return_counts=True)
            if (counts > 1).any():
                raise UsageError(
                    f"input {i + 1} has several values in year {int(unique[counts > 1][0])}; "
                    "keep one value per year first (select or aggregate-temporal)"
                )
            # Bounds and other coords along time describe dates, not years.
            along_time = [c for c in item.coords if c != t and t in item[c].dims]
            item = item.reset_coords(along_time, drop=True)
            item = item.assign_coords({t: years}).rename({t: "year"})
            item["year"].attrs = {"long_name": "calendar year"}
            dss[i] = item

    try:
        out = xr.merge(dss, join="inner", combine_attrs="drop_conflicts")
    except pint.DimensionalityError as exc:
        raise UsageError(f"inputs carry incompatible units on a shared coordinate: {exc}") from None
    except xr.MergeError as exc:
        raise UsageError(
            f"inputs disagree on a coordinate ({exc}); select or drop it on one input first"
        ) from None

    for dim in out.dims:
        largest = max(item.sizes[dim] for item in dss if dim in item.dims)
        if out.sizes[dim] == 0:
            raise UsageError(f"the inputs share no {dim!r} values; nothing to merge")
        if out.sizes[dim] < largest:
            print(
                f"merge: kept the {out.sizes[dim]} of {largest} {dim!r} values every input has",
                file=sys.stderr,
            )
    return out


if __name__ == "__main__":
    merge()

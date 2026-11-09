# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "cftime>=1.6",
# ]
# ///
"""Remove named data variables or non-index coordinates from a Zarr."""

import sys

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.7"


@weather_skill(name="drop", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--name",
    action="append",
    required=True,
    metavar="NAME",
    help="Data variable or coordinate to drop (repeatable); a dim's index only once no data variable spans the dim.",
)
def drop(ds, name, **kwargs):
    """Remove named data variables or coordinates from a Zarr, and dims left without data."""
    names = list(dict.fromkeys(name))
    missing = [n for n in names if n not in ds.variables]
    if missing:
        raise UsageError(
            f"--name {missing} not found; data_vars={list(ds.data_vars)}, coords={list(ds.coords)}"
        )
    if set(ds.data_vars) <= set(names):
        raise UsageError("dropping these names would leave no data variables")
    out = ds.drop_vars([n for n in names if n not in ds.dims])
    # A dim no remaining data variable spans holds no data; its index goes with it.
    used = {d for v in out.data_vars.values() for d in v.dims}
    index = [n for n in names if n in used]
    if index:
        carriers = sorted(v for v in out.data_vars if set(out[v].dims) & set(index))
        raise UsageError(
            f"--name {index} indexes a dim that {carriers} still span; dropping it would "
            "leave the dim without coordinates. Use select to collapse the dim or "
            "summarize-dim to reduce over it."
        )
    orphans = [d for d in out.dims if d not in used]
    if orphans:
        print(f"drop: removed dims {orphans}; no data variable spans them", file=sys.stderr)
    return out.drop_dims(orphans)


if __name__ == "__main__":
    drop()

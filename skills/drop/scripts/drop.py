# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "cftime>=1.6",
# ]
# ///
"""Remove named data variables or non-index coordinates from a Zarr."""

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(name="drop", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--name",
    action="append",
    required=True,
    metavar="NAME",
    help="Data variable or non-index coordinate to drop (repeatable).",
)
def drop(ds, name, **kwargs):
    """Remove named data variables or non-index coordinates from a Zarr."""
    names = list(dict.fromkeys(name))
    missing = [n for n in names if n not in ds.variables]
    if missing:
        raise UsageError(
            f"--name {missing} not found; data_vars={list(ds.data_vars)}, coords={list(ds.coords)}"
        )
    index = [n for n in names if n in ds.dims]
    if index:
        raise UsageError(
            f"--name {index} is a dimension's index coordinate; dropping it would leave the "
            "dim without coordinates. Use select to collapse the dim or summarize-dim to "
            "reduce over it."
        )
    if set(ds.data_vars) <= set(names):
        raise UsageError("dropping these names would leave no data variables")

    return ds.drop_vars(names)


if __name__ == "__main__":
    drop()

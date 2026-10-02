# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
# ]
# ///
"""Drop named data variables or non-index coordinates."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import bounds_of, drop_cf_refs

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


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
    """Drop named data variables or non-index coordinates."""
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

    names += [b for b in bounds_of(ds, names).values() if b not in names and b not in ds.dims]
    ds = ds.drop_vars(names)
    return drop_cf_refs(ds, names)


if __name__ == "__main__":
    drop()

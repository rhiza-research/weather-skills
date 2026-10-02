# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
# ]
# ///
"""Rename variables, coordinates, or dims, and swap a dim onto another coordinate."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import rename_cf_refs
from weather_skills_core.standard_dataset import DIMS, PREDICTION_TIMEDELTA, has_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


def _pair(text):
    old, sep, new = text.partition("=")
    if not (sep and old and new):
        raise UsageError(f"expected OLD=NEW, got {text!r}")
    return old, new


@weather_skill(name="edit-coords", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--swap-dims",
    action="append",
    default=[],
    metavar="OLD_DIM=NEW_DIM",
    help="Index OLD_DIM by its 1-D coordinate NEW_DIM (repeatable). Runs before --rename.",
)
@weather_skill.argument(
    "--rename",
    action="append",
    default=[],
    metavar="OLD=NEW",
    help="Rename a data variable, coordinate, or dim (repeatable).",
)
def edit_coords(ds, swap_dims, rename, **kwargs):
    """Rename variables, coordinates, or dims, and swap a dim onto another coordinate."""
    swaps = dict(map(_pair, swap_dims))
    renames = dict(map(_pair, rename))
    if not swaps and not renames:
        raise UsageError("give at least one --swap-dims or --rename")

    not_coords = [n for n in swaps.values() if n not in ds.coords]
    if not_coords:
        raise UsageError(
            f"--swap-dims: {not_coords} are not coordinates; to rename a dim use --rename"
        )

    before = {d for d in DIMS if has_dim(ds, d)}
    try:
        ds = ds.swap_dims(swaps)
        dupes = [n for n in swaps.values() if not ds.indexes[n].is_unique]
        ds = ds.rename(renames)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    if dupes:
        raise UsageError(f"--swap-dims: {dupes} have duplicate values and cannot index a dim")

    lost = sorted(d for d in before if not has_dim(ds, d))
    if lost:
        hint = ""
        if PREDICTION_TIMEDELTA in lost:
            hint = " For forecast lead → valid time, use step-to-time."
        raise UsageError(f"edit would remove ontology dim(s) {lost}.{hint}")
    return rename_cf_refs(ds, renames)


if __name__ == "__main__":
    edit_coords()

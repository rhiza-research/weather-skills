# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
# ]
# ///
"""Rename variables, coordinates, or dims, and swap a dim onto another coordinate."""

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import bounds_of, rename_cf_refs
from weather_skills_core.standard_dataset import DIMS, PREDICTION_TIMEDELTA, has_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


def _parse_pairs(pairs, flag):
    """``["OLD=NEW", ...]`` → ``{OLD: NEW}``; reject malformed or repeated OLD/NEW."""
    mapping = {}
    for pair in pairs or []:
        old, sep, new = pair.partition("=")
        old, new = old.strip(), new.strip()
        if not sep or not old or not new:
            raise UsageError(f"{flag} expects OLD=NEW, got {pair!r}")
        if old in mapping:
            raise UsageError(f"{flag} names {old!r} more than once")
        mapping[old] = new
    targets = [new for old, new in mapping.items() if old != new]
    dupes = sorted({n for n in targets if targets.count(n) > 1})
    if dupes:
        raise UsageError(f"{flag} maps several names onto {dupes}")
    return mapping


def _names(ds):
    return set(ds.variables) | set(ds.dims)


def _swap(ds, swaps):
    for old_dim, new_dim in swaps.items():
        if old_dim not in ds.dims:
            raise UsageError(f"--swap-dims: {old_dim!r} is not a dim; dims={list(ds.dims)}")
        if new_dim not in ds.coords:
            raise UsageError(
                f"--swap-dims: {new_dim!r} is not a coordinate; coords={list(ds.coords)}"
            )
        if ds[new_dim].dims != (old_dim,):
            raise UsageError(
                f"--swap-dims: {new_dim!r} must be 1-D along {old_dim!r}; "
                f"it has dims {ds[new_dim].dims}"
            )
        values = np.asarray(ds[new_dim].values)
        if len(np.unique(values)) != values.size:
            raise UsageError(
                f"--swap-dims: {new_dim!r} has duplicate values and cannot index {old_dim!r}"
            )
    return ds.swap_dims(swaps) if swaps else ds


def _plan_renames(ds, renames):
    """Validate ``renames`` against ``ds``; add bounds vars named after a renamed coord."""
    existing = _names(ds)
    freed = {old for old, new in renames.items() if old != new}
    for old, new in renames.items():
        if old not in existing:
            raise UsageError(
                f"--rename: {old!r} not found; variables={sorted(ds.variables)}, "
                f"dims={list(ds.dims)}"
            )
        if old != new and new in existing and new not in freed:
            raise UsageError(f"--rename: {new!r} already exists; renaming {old!r} would clash")
    plan = {old: new for old, new in renames.items() if old != new}
    for coord, bounds in bounds_of(ds, plan).items():
        if bounds in renames or not bounds.startswith(coord):
            continue
        new_bounds = plan[coord] + bounds[len(coord) :]
        if new_bounds in existing or new_bounds in plan.values():
            continue
        plan[bounds] = new_bounds
    return plan


@weather_skill(name="edit-coords", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--swap-dims",
    action="append",
    metavar="OLD_DIM=NEW_DIM",
    help="Index OLD_DIM by its 1-D coordinate NEW_DIM (repeatable). Runs before --rename.",
)
@weather_skill.argument(
    "--rename",
    action="append",
    metavar="OLD=NEW",
    help="Rename a data variable, coordinate, or dim (repeatable).",
)
def edit_coords(ds, swap_dims, rename, **kwargs):
    """Rename variables, coordinates, or dims, and swap a dim onto another coordinate."""
    swaps = _parse_pairs(swap_dims, "--swap-dims")
    renames = _parse_pairs(rename, "--rename")
    if not swaps and not renames:
        raise UsageError("give at least one --swap-dims or --rename")

    before = {d for d in DIMS if has_dim(ds, d)}
    ds = _swap(ds, swaps)
    plan = _plan_renames(ds, renames)
    ds = ds.rename(plan) if plan else ds
    rename_cf_refs(ds, plan)

    lost = sorted(d for d in before if not has_dim(ds, d))
    if lost:
        hint = ""
        if PREDICTION_TIMEDELTA in lost:
            hint = " To turn forecast lead times into valid times, use the step-to-time skill."
        raise UsageError(
            f"edit would remove ontology dim(s) {lost}; downstream skills would no longer "
            f"recognise them. Rename to a known name (e.g. lat, lon, time, member).{hint}"
        )
    return ds


if __name__ == "__main__":
    edit_coords()

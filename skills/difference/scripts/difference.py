# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Subtract A − B (xarray-aligned)."""

import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import ensure_normalized_longitude

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_WIDEN = {1: "int16", 2: "int32", 4: "int64"}


@weather_skill(
    name="difference",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--variable", "-v", action="append")
def difference(ds, variable, output, **kwargs):
    """Subtract A − B (xarray-aligned)."""
    if len(ds) != 2:
        raise UsageError(f"expected exactly two --input paths, got {len(ds)}")
    ds_a, ds_b = ds
    ds_a = ensure_normalized_longitude(ds_a)
    ds_b = ensure_normalized_longitude(ds_b)
    import numpy as np
    import xarray as xr

    names = ("A (first input)", "B (second input)")
    vars_ = _select_variables(ds_a, ds_b, variable, names)
    _check_coordinateless_dims(ds_a, ds_b, names)
    out = {}
    for v in vars_:
        a, b = ds_a[v], ds_b[v]
        # Promote bool/uint so A−B cannot wrap
        if a.dtype.kind == "b":
            a = a.astype(np.int16)
        elif a.dtype.kind == "u":
            a = a.astype(getattr(np, _WIDEN.get(a.dtype.itemsize, "int64")))
        if b.dtype.kind == "b":
            b = b.astype(np.int16)
        elif b.dtype.kind == "u":
            b = b.astype(getattr(np, _WIDEN.get(b.dtype.itemsize, "int64")))
        diff = a - b
        _check_not_empty(v, a, b, diff)
        out[v] = diff.assign_attrs(ds_a[v].attrs)
    return xr.Dataset(out)


# The guards below are restored from before #108 (c037ee0), which removed them while SKILL.md kept
# documenting them. Each implements a promise in skills/difference/SKILL.md. (The old units-mismatch
# warning is not restored: weather-skills-core now converts compatible units during the subtraction.)


def _select_variables(ds_a, ds_b, variable, names):
    """--variable names must be data variables of BOTH inputs; by default every shared data variable
    is differenced, and sharing none is an error. Variables not differenced are dropped with a note."""
    if variable:
        selected = list(dict.fromkeys(variable))
        for var in selected:
            absent = [n for n, d in zip(names, (ds_a, ds_b), strict=True) if var not in d.data_vars]
            if absent:
                raise UsageError(
                    f"--variable '{var}' is not a data variable of {absent}. "
                    f"{names[0]} has {list(ds_a.data_vars)}; {names[1]} has {list(ds_b.data_vars)}."
                )
    else:
        selected = [v for v in ds_a.data_vars if v in ds_b.data_vars]
        if not selected:
            raise UsageError(
                f"the inputs share no data variables ({names[0]} has {list(ds_a.data_vars)}; "
                f"{names[1]} has {list(ds_b.data_vars)})."
            )
    dropped = sorted({v for d in (ds_a, ds_b) for v in d.data_vars if v not in selected})
    if dropped:
        print(
            f"Note: dropping data variable(s) {dropped} not differenced "
            "(absent from an input or unselected).",
            file=sys.stderr,
        )
    return selected


def _check_coordinateless_dims(ds_a, ds_b, names):
    """A shared dim without an index coordinate on both sides is paired positionally: exit on a size
    mismatch naming the dim; on equal sizes, warn that the pairing is positional."""
    for d in sorted(set(ds_a.dims) & set(ds_b.dims)):
        if d in ds_a.indexes and d in ds_b.indexes:
            continue
        size_a, size_b = ds_a.sizes[d], ds_b.sizes[d]
        if size_a != size_b:
            raise UsageError(
                f"shared dim '{d}' has no index coordinate, so it is paired positionally, but the "
                f"inputs disagree on its size ({names[0]}={size_a}, {names[1]}={size_b})."
            )
        print(
            f"Warning: shared dim '{d}' has no index coordinate; pairing it positionally "
            f"(element i of {names[0]} minus element i of {names[1]}). Verify the rows correspond.",
            file=sys.stderr,
        )


def _check_not_empty(var, a, b, diff):
    """Exit (no output written) when a variable ends up empty along a dim, naming which of the two
    causes: a dim already empty in an input, or an alignment that found no overlap."""
    empty = [d for d, s in diff.sizes.items() if s == 0]
    if not empty:
        return
    pre_empty = {
        d for d in set(a.sizes) | set(b.sizes) if a.sizes.get(d, 1) == 0 or b.sizes.get(d, 1) == 0
    }
    if set(empty) & pre_empty:
        raise UsageError(
            f"variable '{var}' is already empty along dim(s) {sorted(set(empty) & pre_empty)} "
            "in an input before alignment: there is nothing to subtract."
        )
    raise UsageError(
        f"aligning the inputs left variable '{var}' empty along dim(s) {empty}: the inputs have "
        "no overlapping coordinate values there, so there is nothing to subtract."
    )


if __name__ == "__main__":
    difference()

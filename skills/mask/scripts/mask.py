# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.4.0",
#   "cftime>=1.6",
# ]
# ///
"""Set values to NaN wherever other Zarrs have no value or a 0/1 mask is 0."""

import sys

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


@weather_skill(name="mask", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--valid-in",
    type=Dataset("any"),
    action="append",
    help="Keep only points where this Zarr has a value (repeatable).",
)
@weather_skill.argument(
    "--true-in",
    type=Dataset("any"),
    action="append",
    help="0/1 mask (repeatable): keep only points where it is 1.",
)
@weather_skill.argument("--variable", "-v", help="Data variable to mask (default: all).")
def mask(ds, valid_in, true_in, variable, **kwargs):
    """Set values to NaN wherever other Zarrs have no value or a 0/1 mask is 0."""
    if not valid_in and not true_in:
        raise UsageError("pass at least one --valid-in or --true-in")
    if variable and variable not in ds.data_vars:
        raise UsageError(f"--variable {variable!r} not in input (have {list(ds.data_vars)})")
    ds = dequantify_dataset(ensure_normalized_longitude(ds))

    refs = [(f"--valid-in #{n}", r, False) for n, r in enumerate(valid_in or (), 1)]
    refs += [(f"--true-in #{n}", r, True) for n, r in enumerate(true_in or (), 1)]
    keep = None
    for role, ref, is_mask in refs:
        names = list(ref.data_vars)
        if variable in names:
            name = variable
        elif len(names) == 1:
            name = names[0]
        else:
            raise UsageError(
                f"{role} holds {names}; keep one variable in it with drop, or name the "
                "shared one with --variable"
            )
        r = dequantify_dataset(ensure_normalized_longitude(ref))[name]
        extra = [d for d in r.dims if d not in ds.dims]
        if extra:
            raise UsageError(
                f"{role} has dims {extra} that the input lacks. Dims must match by name: "
                "relabel a forecast's leads with step-to-time, sample a grid at stations "
                "with point-value, or select one entry first"
            )
        ok = r.fillna(0) != 0 if is_mask else r.notnull()
        # A label the reference lacks counts as invalid.
        ok = ok.reindex({d: ds[d] for d in ok.dims if d in ds.coords}, fill_value=False)
        if not ok.any():
            raise DataError(
                f"{role} keeps no point of the input. Labels must match exactly: for lat/lon, "
                "put both on one grid with coarsen --reference-grid; for time, aggregate "
                "both with the same bins"
            )
        try:
            keep = ok if keep is None else keep & ok
        except ValueError as exc:  # unlabeled dims of different sizes
            raise UsageError(f"{role}: cannot line up with the other references: {exc}") from None

    for v in [variable] if variable else list(ds.data_vars):
        missing = [d for d in keep.dims if d not in ds[v].dims]
        if missing:
            raise UsageError(
                f"{v!r} lacks dims {missing} that the references have; name the variables "
                "to mask with --variable"
            )
        try:
            masked = ds[v].where(keep)
        except ValueError as exc:
            raise UsageError(f"{v!r}: cannot line up with the references: {exc}") from None
        masked.attrs = dict(ds[v].attrs)
        before = int(ds[v].notnull().sum())
        print(
            f"{v}: set {before - int(masked.notnull().sum())} of {before} values to NaN",
            file=sys.stderr,
        )
        ds[v] = masked
    return ds


if __name__ == "__main__":
    mask()

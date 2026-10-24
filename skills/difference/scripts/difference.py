# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Subtract one Zarr from another (A − B), cell by cell."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import AGGREGATION_PERIOD_ATTR, looks_like_precip_total, ureg

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.5"

_WIDEN = {1: "int16", 2: "int32", 4: "int64"}


@weather_skill(
    name="difference",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--variable", "-v", action="append")
def difference(ds, variable, **kwargs):
    """Subtract one Zarr from another (A − B), cell by cell."""
    if len(ds) != 2:
        raise UsageError(f"expected exactly two --input paths, got {len(ds)}")
    ds_a, ds_b = ds
    ds_a = ensure_normalized_longitude(ds_a)
    ds_b = ensure_normalized_longitude(ds_b)
    import numpy as np
    import pint
    import xarray as xr

    shared = [v for v in ds_a.data_vars if v in ds_b.data_vars]
    vars_ = list(dict.fromkeys(variable)) if variable else shared
    if not vars_:
        raise UsageError(
            f"inputs share no data variable (A: {list(ds_a.data_vars)}, "
            f"B: {list(ds_b.data_vars)}); rename one to match first with edit-coords "
            "--rename OLD=NEW (e.g. precipitation_surface=precip)"
        )
    for v in vars_:
        if v not in shared:
            raise UsageError(
                f"--variable {v!r} must be in both inputs (A: {list(ds_a.data_vars)}, "
                f"B: {list(ds_b.data_vars)})"
            )
    out = {}
    for v in vars_:
        a, b = ds_a[v], ds_b[v]
        only_a = [d for d in a.dims if d not in b.dims]
        only_b = [d for d in b.dims if d not in a.dims]
        if only_a and only_b:
            # (time) minus (dayofyear) would broadcast into a time x dayofyear cube
            raise UsageError(
                f"{v!r}: A has dims {only_a} that B lacks and B has {only_b} that A lacks; "
                "broadcasting would pair every A label with every B label. Put both on "
                "the same axis first (e.g. step-to-time, or expand a climatology onto "
                "the field's time axis)"
            )
        period_a = a.attrs.get(AGGREGATION_PERIOD_ATTR)
        period_b = b.attrs.get(AGGREGATION_PERIOD_ATTR)
        if (
            period_a
            and period_b
            and (looks_like_precip_total(a) or looks_like_precip_total(b))
            and ureg.Quantity(period_a) != ureg.Quantity(period_b)
        ):
            raise UsageError(
                f"{v!r}: A is a {period_a} total and B a {period_b} total; subtract "
                "totals over the same period (aggregate-temporal both to one --period)"
            )
        # Promote bool/uint so A−B cannot wrap
        if a.dtype.kind == "b":
            a = a.astype(np.int16)
        elif a.dtype.kind == "u":
            a = a.astype(getattr(np, _WIDEN.get(a.dtype.itemsize, "int64")))
        if b.dtype.kind == "b":
            b = b.astype(np.int16)
        elif b.dtype.kind == "u":
            b = b.astype(getattr(np, _WIDEN.get(b.dtype.itemsize, "int64")))
        try:
            diff = a - b
        except pint.DimensionalityError as exc:
            raise UsageError(f"{v!r}: A and B carry incompatible units: {exc}") from None
        except ValueError as exc:  # unlabeled dims of different sizes
            raise UsageError(f"{v!r}: cannot align A and B: {exc}") from None
        empty = [d for d in diff.dims if diff.sizes[d] == 0]
        if empty:
            raise UsageError(
                f"{v!r}: no overlapping {empty} labels between A and B. Labels must match "
                "exactly: for lat/lon, put both on one grid with coarsen --reference-grid "
                "(or downscale); for time, step-to-time a forecast and aggregate both "
                "with the same bins"
            )
        # K − K is a kelvin result; write it as a difference so unit-convert does
        # not read it as an absolute temperature and subtract 273.15.
        units = diff.pint.units
        if (
            units is not None
            and units.dimensionality == "[temperature]"
            and "delta_" not in str(units)
        ):
            diff = diff.pint.to("delta_degC")
        # Replace, not merge: B's attrs (a baseline's aggregation_period or
        # cell_methods) must not leak onto A's field.
        diff.attrs = dict(ds_a[v].attrs)
        # Say what the field is now, so a colorbar does not read as the raw
        # quantity. Always "difference": A - B is all this skill knows; calling
        # it an anomaly is the caller's reading, set in the plot spec.
        # standard_name stays so downstream skills still classify the variable.
        long_name = str(diff.attrs.get("long_name") or v)
        if "difference" not in long_name.lower():
            diff.attrs["long_name"] = f"{long_name} difference"
        out[v] = diff
    return xr.Dataset(out)


if __name__ == "__main__":
    difference()

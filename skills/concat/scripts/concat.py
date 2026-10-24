# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Join two or more Zarrs into one along a named dimension."""

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import names_for

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.5"


def _coerce(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            try:
                out.append(float(v))
            except ValueError:
                try:
                    out.append(np.datetime64(v, "ns"))
                except ValueError:
                    out.append(v)
    return out


@weather_skill(
    name="concat",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--dim", required=True)
@weather_skill.argument("--coords", help="Comma-separated coord values for the new dim")
def concat(ds, dim, coords, **kwargs):
    """Join two or more Zarrs into one along a named dimension."""
    import pint
    import xarray as xr

    # Repeat -i/--input per store. nargs="+" would treat a second -i as a
    # replacement, so only the last input would be written.
    if not isinstance(ds, (list, tuple)) or len(ds) < 2:
        n = 1 if not isinstance(ds, (list, tuple)) else len(ds)
        raise UsageError(f"concat requires at least two --input paths (repeat -i), got {n}")
    dss = list(ds)

    has_dim = [dim in item.dims for item in dss]
    if any(has_dim) and not all(has_dim):
        raise UsageError(
            f"{dim!r} is a dim on some inputs but not others; concat needs it on all or none"
        )
    if all(has_dim) and coords:
        raise UsageError(
            f"--coords labels a new dim; {dim!r} already exists on the inputs, so its own labels are kept"
        )

    # xr.concat pads a variable missing from some inputs with NaN, which looks
    # like missing data; refuse instead.
    names = [set(item.data_vars) for item in dss]
    if any(n != names[0] for n in names):
        missing = {
            f"input {i + 1}": sorted(set.union(*names) - n)
            for i, n in enumerate(names)
            if n != set.union(*names)
        }
        raise UsageError(
            f"inputs carry different variables (missing: {missing}); every input needs the "
            "same variables (rename to match, or refetch with the same -v)"
        )

    # Mixing e.g. daily and weekly values on one axis would keep only the
    # first input's label, so refuse instead.
    for key in ("data_interval", "aggregation_period"):
        for name in dss[0].data_vars:
            seen = {str(item[name].attrs.get(key)) for item in dss if name in item.data_vars}
            if len(seen) > 1:
                raise UsageError(f"{name!r} has different {key} across inputs: {sorted(seen)}")

    # A standard and a model calendar (noleap, 360_day) cannot share one axis.
    calendars = set()
    for item in dss:
        if dim in item.coords and item[dim].dtype.kind in "MO":
            try:
                calendars.add(item[dim].dt.calendar)
            except (AttributeError, TypeError):
                pass
    if len(calendars) > 1:
        raise UsageError(
            f"{dim!r} uses different calendars across inputs ({', '.join(sorted(calendars))}); "
            "put them on one calendar first with convert-calendar"
        )

    # Single-value labels, such as the time or step that select keeps.
    # If only some inputs have one (say valid_time), xr.concat would copy the
    # first input's value onto every input: a wrong date. Drop those labels.
    single_labels = [{c for c in item.coords if item[c].ndim == 0} for item in dss]
    on_every_input = set.intersection(*single_labels)
    on_some_inputs = set.union(*single_labels) - on_every_input
    dss = [item.drop_vars(on_some_inputs & set(item.coords)) for item in dss]

    if not all(has_dim):
        if coords:
            vals = _coerce([c.strip() for c in coords.split(",")])
            if len(vals) != len(dss):
                raise UsageError(f"--coords len {len(vals)} != inputs {len(dss)}")
            dss = [d.expand_dims({dim: [v]}) for d, v in zip(dss, vals, strict=True)]
        else:
            dss = [d.expand_dims(dim) for d in dss]

    # join="exact": every other coordinate must match. The outer join pads a
    # mismatch with NaN instead, which looks like missing data.
    # coords="different": a single-value label that differs between inputs
    # (ECMWF step 7 days, GEFS step 14 days) becomes a label along dim.
    try:
        out = xr.concat(dss, dim=dim, join="exact", coords="different")
    except pint.DimensionalityError as exc:
        raise UsageError(f"inputs carry incompatible units: {exc}") from None
    except xr.AlignmentError as exc:
        raise UsageError(
            f"inputs differ along a dim other than {dim!r} ({exc}). Put them on one grid and window first: "
            "coarsen --reference-grid for lat/lon, select or clip-region to trim."
        ) from None

    if dim in out.indexes:
        index = out.indexes[dim]
        if index.has_duplicates:
            dups = index[index.duplicated()].unique()
            raise UsageError(
                f"inputs overlap along {dim!r}: {[str(v) for v in dups[:5]]} appear more than once; "
                "keep only the new entries of one input with select (repeat --index)"
            )
        if not (index.is_monotonic_increasing or index.is_monotonic_decreasing):
            out = out.sortby(dim)
        # Boxes stitched along lat/lon with a gap between them leave an
        # irregular axis that clipping, regridding, and plotting assume away.
        if dim in names_for("lat") + names_for("lon") and out.sizes[dim] > 2:
            steps = np.abs(np.diff(out[dim].values.astype(float)))
            if not np.allclose(steps, steps[0], rtol=1e-3):
                raise UsageError(
                    f"stitched {dim!r} is not evenly spaced (steps {sorted({float(round(v, 6)) for v in steps})}); "
                    "the inputs leave a gap or sit on different grids"
                )
    return out


if __name__ == "__main__":
    concat()

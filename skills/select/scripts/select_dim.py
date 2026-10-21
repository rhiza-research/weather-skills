# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "pandas",
# ]
# ///
"""Pick entries along one dimension of a Zarr by position or by coordinate value."""

import re

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    format_duration,
    interval_days,
    ureg,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

_KEEP_SCALAR_COORDS = frozenset(
    {
        "station_id",
        "point_id",
        "name",
        "latitude",
        "lat",
        "longitude",
        "lon",
        "country",
        # The selected bin's coverage, so convert-to-totals can still refuse a
        # partial interval after the time/step axis is gone.
        AGGREGATION_COVERAGE_COORD,
    }
)
_INDEX_RE = re.compile(r"-?[0-9]+")
_NUM_INT_RE = re.compile(r"-?[0-9]+")
_NUM_FLOAT_RE = re.compile(r"-?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?")
_CFTIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2})(?::(\d{2})(?::(\d{2}))?)?)?")


def _is_string_coord(dtype, coord_vals) -> bool:
    """True for unicode, bytes, NumPy 2 StringDType (kind 'T'), or object-of-str."""
    if dtype.kind in "UST":
        return True
    return dtype.kind == "O" and coord_vals.size and isinstance(coord_vals.flat[0], str)


def _parse_value(raw, coord_vals, dim):
    import numpy as np
    import pandas as pd

    dtype = coord_vals.dtype
    if np.issubdtype(dtype, np.datetime64):
        # Reject relative/timezone forms — numpy would silently convert them
        if not raw or raw.lower() in {"now", "today"} or raw[-1:] in "Zz":
            raise UsageError(f"--value '{raw}' not a naive ISO datetime")
        parts = re.split(r"[T ]", raw, maxsplit=1)
        if len(parts) == 2 and re.search(r"[+-]", parts[1]):
            raise UsageError(f"--value '{raw}' not a naive ISO datetime")
        try:
            return np.datetime64(raw)
        except ValueError:
            raise UsageError(
                f"--value '{raw}' is not an ISO datetime (e.g. 2026-06-01, 2026-06-01T06:00)"
            ) from None
    if np.issubdtype(dtype, np.timedelta64):
        try:
            return pd.Timedelta(raw).to_timedelta64()
        except ValueError:
            raise UsageError(f"--value '{raw}' is not a timedelta (e.g. 7D, 168h)") from None
    first = coord_vals.flat[0] if coord_vals.size else None
    if dtype.kind == "O" and hasattr(first, "calendar"):
        # cftime labels (model calendars): build the date in the coord's own
        # calendar, so 360_day dates like 2026-02-30 are selectable.
        import cftime

        match = _CFTIME_RE.fullmatch(raw)
        if match is None:
            raise UsageError(f"--value '{raw}' is not an ISO datetime (e.g. 2026-06-01)")
        fields = [int(f) for f in match.groups() if f is not None]
        try:
            return cftime.datetime(*fields, calendar=first.calendar)
        except ValueError as exc:
            raise UsageError(f"--value '{raw}' is not a {first.calendar} date: {exc}") from None
    if np.issubdtype(dtype, np.number):
        if _NUM_INT_RE.fullmatch(raw):
            return int(raw)
        if _NUM_FLOAT_RE.fullmatch(raw):
            return float(raw)
        raise UsageError(f"--value '{raw}' not a number literal")
    if _is_string_coord(dtype, coord_vals):
        return raw.encode() if dtype.kind == "S" else raw
    raise UsageError(f"coord '{dim}' dtype {dtype} needs --index, not --value")


@weather_skill(
    name="select",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--dim", required=True, action="append", help="Dimension to select along (once)."
)
@weather_skill.argument("--index", action="append", help="Integer position (repeatable).")
@weather_skill.argument("--value", action="append", help="Coord value (repeatable).")
def select(ds, dim, index, value, **kwargs):
    """Pick entries along one dimension of a Zarr by position or by coordinate value."""
    # action="append" so a second --dim is refused instead of silently replacing the first.
    if len(dim) > 1:
        raise UsageError(f"--dim may be given once; got {dim}. Select one dim per run")
    dim = dim[0]
    import numpy as np

    if (index is None) == (value is None):
        raise UsageError("exactly one of --index or --value is required")
    if dim not in ds.dims:
        raise UsageError(f"--dim '{dim}' is not a dimension; dims are {list(ds.dims)}")
    size = ds.sizes[dim]
    if index is not None:
        positions = []
        seen = {}
        for raw in index:
            if not _INDEX_RE.fullmatch(raw):
                raise UsageError(f"--index '{raw}' is not an integer")
            pos = int(raw)
            if not -size <= pos < size:
                raise UsageError(f"--index {raw} is out of range for '{dim}' (size {size})")
            canon = pos % size
            if canon in seen:
                raise UsageError(f"--index {seen[canon]} and {raw} address the same element")
            seen[canon] = raw
            positions.append(pos)
    else:
        if dim not in ds.coords:
            raise UsageError(f"'{dim}' has no coordinate values; select it with --index")
        coord_vals = ds[dim].values
        positions, seen = [], {}
        for raw in value:
            target = _parse_value(raw, coord_vals, dim)
            if np.issubdtype(coord_vals.dtype, np.floating):
                # Float labels built by arithmetic (0.30000000000000004) still match 0.3.
                hits = np.isclose(coord_vals, target, rtol=1e-9, atol=1e-9)
            else:
                hits = coord_vals == target
            matched = np.nonzero(hits)[0]
            if matched.size == 0:
                head = coord_vals[:5]
                if np.issubdtype(head.dtype, np.datetime64):
                    head = np.datetime_as_string(head, unit="auto")
                sample = ", ".join(str(v) for v in head)
                more = f", ... ({size} total)" if size > 5 else ""
                raise UsageError(f"--value '{raw}' is not on '{dim}'; values are {sample}{more}")
            if matched.size > 1:
                raise UsageError(
                    f"--value '{raw}' matches {matched.size} entries on '{dim}' "
                    "(duplicate labels); select with --index"
                )
            pos = int(matched[0])
            if pos in seen:
                raise UsageError(f"--value '{seen[pos]}' and '{raw}' address the same element")
            seen[pos] = raw
            positions.append(pos)

    if len(positions) == 1:
        pre_scalar = {c for c in ds.coords if ds[c].ndim == 0}
        out = ds.isel({dim: positions[0]})
        # The selected label (and its valid_time) stays as a scalar coord, so
        # verify and plot still know which date or lead this is.
        keep = _KEEP_SCALAR_COORDS | {dim, "valid_time"}
        drop = [c for c in out.coords if out[c].ndim == 0 and c not in pre_scalar and c not in keep]
        out = out.drop_vars(drop)
        # A calendar aggregation_period ("1 month") is a different length per
        # label. The label is dropped here, so stamp this bin's real length.
        for name in out.data_vars:
            period = out[name].attrs.get(AGGREGATION_PERIOD_ATTR)
            calendar = isinstance(period, str) and re.search(r"month|year", period)
            if calendar and ds[dim].dtype.kind in "MO":  # datetime64 or cftime labels
                days = interval_days(ds[dim].isel({dim: [positions[0]]}), period)[0]
                out[name].attrs[AGGREGATION_PERIOD_ATTR] = format_duration(
                    ureg.Quantity(float(days), "day")
                )
        return out
    return ds.isel({dim: positions})


if __name__ == "__main__":
    select()

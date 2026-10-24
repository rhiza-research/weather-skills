# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
# ]
# ///
"""Convert a Zarr's time axis to another CF calendar."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_time_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"


@weather_skill(
    name="convert-calendar",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--calendar",
    required=True,
    choices=[
        "standard",
        "gregorian",
        "proleptic_gregorian",
        "noleap",
        "365_day",
        "360_day",
        "all_leap",
        "366_day",
        "julian",
    ],
)
@weather_skill.argument(
    "--align-on",
    choices=["date", "year"],
    help="Required when source or target is 360_day.",
)
@weather_skill.argument("--time-dim")
def convert_calendar(ds, calendar, align_on, time_dim, **kwargs):
    """Convert a Zarr's time axis to another CF calendar."""
    import sys

    import numpy as np
    import pandas as pd

    time_dim = detect_time_dim(ds, time_dim)
    vals = np.asarray(ds[time_dim].values)
    if vals.dtype.kind == "M":
        source = "standard"
    elif vals.size and hasattr(vals.flat[0], "calendar"):
        source = vals.flat[0].calendar
    else:
        raise UsageError(
            f"{time_dim!r} holds {vals.dtype} values, not dates. convert-calendar needs a "
            "wall-clock time axis; for a forecast lead axis run step-to-time first."
        )
    # xarray requires align_on when 360_day is involved
    if (source == "360_day" or calendar == "360_day") and align_on is None:
        raise UsageError("--align-on required when source or target is 360_day")

    # CF bounds share the time axis's calendar. Carry them over as offsets from
    # each label, so every cell keeps its exact width on the new calendar.
    bounds = ds[time_dim].attrs.get("bounds")
    if bounds not in ds.variables:
        bounds = None
    if bounds is not None:
        ds = ds.assign_coords({bounds: ds[bounds] - ds[time_dim]})

    n = vals.size
    ds = ds.assign_coords(_source_row=(time_dim, np.arange(n)))
    out = ds.convert_calendar(calendar, dim=time_dim, align_on=align_on)
    kept = out["_source_row"].values
    out = out.drop_vars("_source_row")

    dropped = np.setdiff1d(np.arange(n), kept)
    if dropped.size == n:
        raise UsageError(
            f"none of the {n} {time_dim!r} values exist in the {calendar} calendar; "
            "nothing to write."
        )
    if dropped.size:
        listed = ", ".join(str(v) for v in pd.Index(vals[dropped])[:10])
        listed += ", ..." if dropped.size > 10 else ""
        print(
            f"Note: dropped {dropped.size} of {n} {time_dim} value(s) not representable "
            f"in the {calendar} calendar: {listed}",
            file=sys.stderr,
        )

    if bounds is not None:
        labels = out.indexes[time_dim]
        offsets = out[bounds].values
        edges = [
            np.asarray(labels + pd.to_timedelta(offsets[:, k])) for k in range(offsets.shape[1])
        ]
        out = out.assign_coords({bounds: (out[bounds].dims, np.stack(edges, axis=1))})
    return out


if __name__ == "__main__":
    convert_calendar()

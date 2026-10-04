# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime>=1.6",
#   "numpy",
#   "xarray",
# ]
# ///
"""Extract day-of-year (1-366) from a datetime64 data variable.

For each selected data variable, replaces it with its calendar day of year
via xarray's ``.dt.dayofyear`` accessor, under a new ``VAR_dayofyear`` name.
Element-wise (no dim is reduced): the result keeps the source variable's
exact shape and dims. ``NaT`` entries become ``NaN``. Data variables that
aren't selected pass through untouched.

Only ``datetime64``-typed variables qualify — a lead-time/duration
(``timedelta64``) variable, e.g. the ``step`` axis or an onset-date result
still expressed as elapsed lead time, has no calendar day of year to read;
run ``step-to-time`` first to turn it into an absolute date.
"""

import sys

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"


def _is_datetime64(da):
    import numpy as np

    return np.issubdtype(da.dtype, np.datetime64)


@weather_skill(
    name="day-of-year",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help="Restrict the computation to this data variable. Repeatable. Each "
    "selected variable must be datetime64-typed. Default (unset): every "
    "datetime64-typed data variable.",
)
def day_of_year(ds, variable, **kwargs):
    """Extract day-of-year (1-366) from a datetime64 data variable."""
    # Variable selection, mirroring `spell-length`/`onset-date`: explicit
    # --variable names must be data variables and must each be
    # datetime64-typed. Default selection takes every datetime64-typed data
    # variable; the rest pass through untouched.
    if variable is not None:
        data_vars = list(ds.data_vars)
        invalid = [v for v in variable if v not in ds.data_vars]
        if invalid:
            raise UsageError(
                f"--variable {invalid} not data variable(s) of the input. "
                f"Valid data variables: {data_vars}"
            )
        selected = list(dict.fromkeys(variable))
        not_datetime = [v for v in selected if not _is_datetime64(ds[v])]
        if not_datetime:
            dtypes = [str(ds[v].dtype) for v in not_datetime]
            raise UsageError(
                f"variable(s) {not_datetime} are not datetime64-typed (dtypes: "
                f"{dtypes}). A lead-time/duration (timedelta64) variable has no "
                "calendar day of year; run step-to-time first to convert it to "
                "an absolute date."
            )
    else:
        selected = [v for v in ds.data_vars if _is_datetime64(ds[v])]
        if not selected:
            dtypes = {v: str(ds[v].dtype) for v in ds.data_vars}
            raise UsageError(f"no datetime64-typed data variable found (dtypes: {dtypes}).")

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through untouched data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    print(f"Computing day-of-year for variables={selected}", file=sys.stderr)

    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        if getattr(da, "pint", None) is not None and da.pint.units is not None:
            da = da.pint.dequantify()

        result = da.dt.dayofyear
        # Attrs are rebuilt from scratch, NOT carried over from the source
        # variable: the source describes a date, not this derived integer
        # day-of-year. standard_name is explicitly None (CF has no entry for
        # this derived quantity); units is the CF convention "1" for a
        # dimensionless count (same reasoning as spell-length).
        result.attrs = {
            "GRIB_name": f"{var} day of year",
            "long_name": f"{var} day of year",
            "description": f"day of year (1-366; NaT -> NaN) extracted from {var}",
            "standard_name": None,
            "units": "1",
        }
        del out_ds[var]
        out_ds[f"{var}_dayofyear"] = result

    return out_ds


if __name__ == "__main__":
    day_of_year()

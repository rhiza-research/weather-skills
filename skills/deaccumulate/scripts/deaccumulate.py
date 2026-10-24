# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Convert a forecast variable stored as a running total since init into per-step values."""

import sys

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import deaccumulate_along_step

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"


@weather_skill(
    name="deaccumulate",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("forecast"), required=True)
@weather_skill.argument("--variable", "-v")
def deaccumulate(ds, variable, **kwargs):
    """Convert a forecast variable stored as a running total since init into per-step values."""
    if variable is None:
        if len(ds.data_vars) != 1:
            raise UsageError(
                f"input has {len(ds.data_vars)} data vars {list(ds.data_vars)}; pass --variable"
            )
        variable = next(iter(ds.data_vars))
    if variable not in ds.data_vars:
        raise UsageError(f"variable {variable!r} not in dataset (have {list(ds.data_vars)})")
    da = ds[variable].pint.dequantify()
    # Increments are clipped at zero, so a signed or downward accumulation
    # (e.g. ECMWF evaporation or net thermal radiation) would become all zeros.
    # The relative tolerance lets GRIB packing noise around zero through.
    if np.nanmin(da.values) < -1e-6 * np.nanmax(np.abs(da.values)):
        raise UsageError(
            f"{variable!r} has negative values; deaccumulate clips increments at zero "
            "and supports only non-negative accumulations (precip, downward radiation)"
        )
    if not da.attrs.get("units") and not da.attrs.get("standard_name"):
        print(
            f"Warning: {variable!r} has no units or standard_name; "
            "cannot check that it is cumulative",
            file=sys.stderr,
        )
    return deaccumulate_along_step(ds, names=[variable])


if __name__ == "__main__":
    deaccumulate()

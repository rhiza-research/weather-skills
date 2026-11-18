# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "cftime>=1.6",
# ]
# ///
"""Limit data variable values to a range: below --min become --min, above --max become --max."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(name="clip-values", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", help="Data variable to clip (default: all).")
@weather_skill.argument(
    "--min", type=float, dest="vmin", help="Lower limit, in the variable's units."
)
@weather_skill.argument(
    "--max", type=float, dest="vmax", help="Upper limit, in the variable's units."
)
def clip_values(ds, variable, vmin, vmax, **kwargs):
    """Limit data variable values to a range: below --min become --min, above --max become --max."""
    if vmin is None and vmax is None:
        raise UsageError("pass --min, --max, or both")
    if vmin is not None and vmax is not None and vmin > vmax:
        raise UsageError(f"--min {vmin} is greater than --max {vmax}")
    if variable and variable not in ds.data_vars:
        raise UsageError(f"--variable {variable!r} not in dataset (have {list(ds.data_vars)})")

    # Limits are plain numbers in each variable's own units, so compare unquantified values.
    ds = dequantify_dataset(ds)
    for name in [variable] if variable else list(ds.data_vars):
        ds[name] = ds[name].clip(vmin, vmax, keep_attrs=True)
    return ds


if __name__ == "__main__":
    clip_values()

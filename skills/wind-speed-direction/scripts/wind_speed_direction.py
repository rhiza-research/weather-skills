# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.2",
# ]
# ///
"""Compute wind speed and from / to directions from u and v wind components."""

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import dequantify_dataset, units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


@weather_skill(name="wind-speed-direction", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--u", required=True, help="Eastward wind component variable (u10, u).")
@weather_skill.argument("--v", required=True, help="Northward wind component variable (v10, v).")
@weather_skill.argument(
    "--calm-speed",
    type=float,
    default=1e-6,
    help="Speeds at or below this, in the units of u and v, count as calm (direction NaN).",
)
def wind_speed_direction(ds, u, v, calm_speed, **kwargs):
    """Compute wind speed and from / to directions from u and v wind components."""
    # dequantify_dataset keeps each variable's units attr (Dataset.map drops it).
    ds = dequantify_dataset(ds)
    missing = [name for name in (u, v) if name not in ds.data_vars]
    if missing:
        raise UsageError(f"{missing} not in dataset (have {list(ds.data_vars)})")
    if not calm_speed >= 0:
        raise UsageError(f"--calm-speed must be zero or more, got {calm_speed}")
    u_da, v_da = ds[u], ds[v]

    units = u_da.attrs.get("units")
    if not (isinstance(units, str) and units.strip()):
        raise UsageError(f"variable {u!r} has no units attr")
    if not units_equal(units, v_da.attrs.get("units")):
        raise UsageError(
            f"{u!r} is in {units!r} but {v!r} is in {v_da.attrs.get('units')!r}; "
            "convert them to one unit first"
        )
    # Variables in one dataset share coordinate values, so only their dims can differ.
    if u_da.dims != v_da.dims:
        raise UsageError(f"{u!r} has dims {u_da.dims} but {v!r} has dims {v_da.dims}")

    speed = np.hypot(u_da, v_da)
    # arctan2(u, v) is the bearing clockwise from north the wind blows towards.
    # Adding 360 before the modulo keeps tiny negative angles from landing on 360.
    to_dir = (np.degrees(np.arctan2(u_da, v_da)) + 360.0) % 360.0
    from_dir = (to_dir + 180.0) % 360.0
    # Calm cells, at or below --calm-speed, have no direction.
    to_dir = to_dir.where(speed > calm_speed)
    from_dir = from_dir.where(speed > calm_speed)

    out = xr.Dataset(
        {"wind_speed": speed, "wind_from_direction": from_dir, "wind_to_direction": to_dir},
        attrs=ds.attrs,
    )
    out["wind_speed"].attrs = {
        "units": units,
        "standard_name": "wind_speed",
        "long_name": f"wind speed from {u} and {v}",
    }
    out["wind_from_direction"].attrs = {
        "units": "degree",
        "standard_name": "wind_from_direction",
        "long_name": "direction the wind blows from, clockwise from north",
    }
    out["wind_to_direction"].attrs = {
        "units": "degree",
        "standard_name": "wind_to_direction",
        "long_name": "direction the wind blows to, clockwise from north",
    }
    return out


if __name__ == "__main__":
    wind_speed_direction()

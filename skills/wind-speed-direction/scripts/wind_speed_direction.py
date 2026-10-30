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
from weather_skills_core.cf import cf_dim
from weather_skills_core.units import dequantify_dataset, units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# Arrow heads, as in Plotly's quiver: two barbs at 20° to the shaft, 0.3 of its length.
HEAD_ANGLE = np.radians(20.0)
HEAD_FRACTION = 0.3


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
@weather_skill.argument(
    "--arrow-length",
    type=float,
    help="Also write quiver arrows (arrow_x / arrow_y, line segments to plot as lines), sized "
    "so a strong wind for the file (the 95th percentile of speeds) spans this many arrow "
    "spacings; 2 is a good start.",
)
@weather_skill.argument(
    "--arrow-scale",
    type=float,
    help="Instead of --arrow-length, a fixed arrow size in degrees per unit of speed (0.08 for "
    "m s-1 draws a 10 m s-1 wind 0.8° long), e.g. to give several files one scale.",
)
@weather_skill.argument(
    "--arrow-step",
    type=int,
    default=1,
    help="The spacing of the arrows in grid cells: 3 puts one arrow on every 3rd cell in "
    "latitude and longitude (every 3rd station for station data).",
)
def wind_speed_direction(ds, u, v, calm_speed, arrow_length, arrow_scale, arrow_step, **kwargs):
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
    if arrow_length is None and arrow_scale is None:
        return out
    if arrow_length is not None and arrow_scale is not None:
        raise UsageError("pass --arrow-length or --arrow-scale, not both")
    size = arrow_length if arrow_length is not None else arrow_scale
    if not size > 0 or arrow_step < 1:
        raise UsageError(
            f"--arrow-length / --arrow-scale must be above 0 and --arrow-step at least 1 "
            f"(got {size}, {arrow_step})"
        )
    lat, lon = cf_dim(ds, "latitude"), cf_dim(ds, "longitude")
    if lat is None or lon is None:
        raise UsageError("arrows need latitude and longitude coordinates")

    # The arrow positions: every Nth cell of a grid, or every Nth station.
    if lat in u_da.dims and lon in u_da.dims:
        picks = {lat: slice(None, None, arrow_step), lon: slice(None, None, arrow_step)}
        ua, va = u_da.isel(picks), v_da.isel(picks)
        ua, va = ua.stack(arrow=(lat, lon)), va.stack(arrow=(lat, lon))
    else:
        point = u_da[lon].dims[0]
        ua = u_da.isel({point: slice(None, None, arrow_step)}).rename({point: "arrow"})
        va = v_da.isel({point: slice(None, None, arrow_step)}).rename({point: "arrow"})
    x0 = ua["arrow"][lon] if lon in ua["arrow"].coords else ua[lon]
    y0 = ua["arrow"][lat] if lat in ua["arrow"].coords else ua[lat]
    x0, y0 = np.asarray(x0.values, float), np.asarray(y0.values, float)
    other = [d for d in ua.dims if d != "arrow"]
    ua, va = ua.transpose(*other, "arrow"), va.transpose(*other, "arrow")

    if arrow_length is not None:
        # The arrow spacing in degrees: the grid spacing times --arrow-step (the tighter of
        # latitude and longitude), or for stations the median distance to the nearest one.
        if lat in u_da.dims and lon in u_da.dims:
            # A dim with one value (a single row of cells) has no spacing of its own.
            steps = [
                np.median(np.abs(np.diff(np.asarray(u_da[d].values, float))))
                for d in (lat, lon)
                if u_da.sizes[d] > 1
            ]
            if not steps:
                raise UsageError("--arrow-length needs two or more grid cells; use --arrow-scale")
            spacing = arrow_step * min(steps)
        else:
            if x0.size < 2:
                raise UsageError("--arrow-length needs two or more stations; use --arrow-scale")
            apart = np.hypot(x0[:, None] - x0[None, :], y0[:, None] - y0[None, :])
            np.fill_diagonal(apart, np.inf)
            spacing = float(np.median(apart.min(axis=1)))
        # A strong wind for the file, the 95th percentile of the arrows' speeds over every
        # time, step and member, spans arrow_length spacings, so every map drawn from it
        # shares one scale; one extreme cell doesn't shrink all the other arrows.
        speeds = np.hypot(ua.values, va.values)
        strong = float(np.nanpercentile(speeds, 95)) if np.isfinite(speeds).any() else 0.0
        arrow_scale = arrow_length * spacing / strong if strong > calm_speed else 0.0

    # Each arrow is 7 vertices: tail, tip, gap, barb, tip, barb, gap. NaN gaps split the
    # arrows, so all of them draw as one line trace. A calm cell is all NaN.
    dx, dy = arrow_scale * ua.values, arrow_scale * va.values
    calm = np.hypot(ua.values, va.values) <= calm_speed
    dx, dy = np.where(calm, np.nan, dx), np.where(calm, np.nan, dy)
    tip_x, tip_y = x0 + dx, y0 + dy
    back = np.arctan2(-dy, -dx)
    head = HEAD_FRACTION * np.hypot(dx, dy)
    gap = np.full(dx.shape, np.nan)
    tail_x = np.where(calm, np.nan, np.broadcast_to(x0, dx.shape))
    tail_y = np.where(calm, np.nan, np.broadcast_to(y0, dy.shape))
    xs = [
        tail_x,
        tip_x,
        gap,
        tip_x + head * np.cos(back + HEAD_ANGLE),
        tip_x,
        tip_x + head * np.cos(back - HEAD_ANGLE),
        gap,
    ]
    ys = [
        tail_y,
        tip_y,
        gap,
        tip_y + head * np.sin(back + HEAD_ANGLE),
        tip_y,
        tip_y + head * np.sin(back - HEAD_ANGLE),
        gap,
    ]
    shape = (*dx.shape[:-1], dx.shape[-1] * 7)
    coords = {d: ua[d] for d in other}
    dims = (*other, "arrow_vertex")
    for name, parts, axis in (("arrow_x", xs, "longitude"), ("arrow_y", ys, "latitude")):
        values = np.stack(parts, axis=-1).reshape(shape)
        out[name] = xr.DataArray(values, dims=dims, coords=coords)
        out[name].attrs = {
            "units": "degrees_east" if axis == "longitude" else "degrees_north",
            "long_name": f"quiver arrow {axis}s: tail, tip, gap, barb, tip, barb, gap per arrow",
            "arrow_scale": f"{arrow_scale:.6g} degree per {units}",
            "arrow_step": arrow_step,
            **({"arrow_length": arrow_length} if arrow_length is not None else {}),
        }
    return out


if __name__ == "__main__":
    wind_speed_direction()

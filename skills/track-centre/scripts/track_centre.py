# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
#   "shapely",
# ]
# ///
"""Follow one low or high through each ensemble member's forecast and build its funnel."""
# From --start, each member's centre is the smoothed extreme within --search at
# the first lead, then within --max-move of the previous centre at each later
# lead. A track ends when the centre falls on the edge of its search window or
# is less than --min-depth below (above, for a high) the mean of a 2-4 degree
# ring around it. Per lead, the funnel is the circle around the members' mean
# position holding --funnel percent of them; the funnel polygon joins the
# circles of consecutive leads.

import json
import sys
from pathlib import Path

import numpy as np
import xarray as xr
from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.ops import unary_union
from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# The ring whose mean a centre must sit --min-depth below (above, for a high).
RING_DEG = (2.0, 4.0)
KM_PER_DEG = 111.2


@weather_skill(name="track-centre", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument(
    "--start", required=True, help="LAT/LON near the centre at the first lead, e.g. -5.8/49.2."
)
@weather_skill.argument("--kind", choices=["low", "high"], default="low")
@weather_skill.argument("--variable", "-v", help="Field to track (default: the only variable).")
@weather_skill.argument(
    "--search", type=float, default=3.0, help="Degrees around --start to look at the first lead."
)
@weather_skill.argument(
    "--max-move", type=float, default=2.5, help="Degrees a centre may move per 6 hours."
)
@weather_skill.argument(
    "--min-depth",
    type=float,
    default=1.0,
    help="Variable units (hPa for MSLP) the centre must sit below the 2-4 degree ring mean.",
)
@weather_skill.argument(
    "--smooth", type=float, default=0.5, help="Degrees: half-width of the box smoothing."
)
@weather_skill.argument(
    "--funnel",
    type=float,
    default=90.0,
    help="Percent of tracked members each funnel circle holds.",
)
@weather_skill.argument(
    "--min-members",
    type=float,
    default=15.0,
    help="Percent of members a lead needs for a funnel circle (and at least 5).",
)
@weather_skill.argument(
    "--every",
    default="6h",
    help="Track at leads this far apart (a timedelta: 6h, 12h); others are skipped. Default 6h.",
)
@weather_skill.argument("--funnel-geojson", help="Also write the funnel polygon as GeoJSON here.")
def track_centre(
    ds,
    start,
    kind,
    variable,
    search,
    max_move,
    min_depth,
    smooth,
    funnel,
    min_members,
    every,
    funnel_geojson,
    **kwargs,
):
    """Follow one low or high through each ensemble member's forecast and build its funnel."""
    import pandas as pd

    try:
        start_lat, start_lon = (float(v) for v in start.split("/"))
    except ValueError:
        raise UsageError(f"--start must be LAT/LON, got {start!r}") from None
    for name, value in (("--search", search), ("--max-move", max_move), ("--smooth", smooth)):
        if not np.isfinite(value) or value <= 0:
            raise UsageError(f"{name} must be a positive number of degrees")
    try:
        every = pd.Timedelta(every).to_timedelta64()
    except ValueError:
        raise UsageError(f"--every must be a timedelta such as 6h, got {every!r}") from None
    if every <= np.timedelta64(0, "ns"):
        raise UsageError("--every must be positive")
    if not 0 < funnel <= 100 or not 0 <= min_members <= 100:
        raise UsageError("--funnel and --min-members are percentages (0-100)")
    if variable is None:
        if len(ds.data_vars) != 1:
            raise UsageError(f"pass --variable; the input has {sorted(ds.data_vars)}")
        variable = next(iter(ds.data_vars))
    if variable not in ds.data_vars:
        raise UsageError(f"--variable {variable!r} not in the input; have {sorted(ds.data_vars)}")

    lat_dim, lon_dim = detect_spatial_dims(ds)
    ds = dequantify_dataset(ensure_normalized_longitude(ds, lon_dim))
    da = ds[variable].sortby(lat_dim).sortby(lon_dim)
    lead_dim = "step" if "step" in da.dims else "time" if "time" in da.dims else None
    if lead_dim is None:
        raise UsageError("the input needs a step (forecast lead) or time dim to track along")
    if "number" not in da.dims:
        da = da.expand_dims(number=[0])
    extra = [d for d in da.dims if d not in ("number", lead_dim, lat_dim, lon_dim)]
    if extra:
        raise UsageError(f"select one value of {extra} first (select skill)")
    da = da.transpose("number", lead_dim, lat_dim, lon_dim)
    # Track on a fixed interval, so hourly and 6-hourly ensembles are checked at
    # the same leads and one jumpy hour does not end a track.
    offset = da[lead_dim].values - (
        da[lead_dim].values[0] if lead_dim == "time" else np.timedelta64(0, "ns")
    )
    da = da.isel({lead_dim: np.flatnonzero(offset % every == np.timedelta64(0, "ns"))})
    if da.sizes[lead_dim] == 0:
        raise UsageError(f"no lead is a multiple of --every {every}; pass a smaller --every")
    lat = da[lat_dim].values.astype(float)
    lon = da[lon_dim].values.astype(float)
    glat, glon = np.meshgrid(lat, lon, indexing="ij")
    cell = float(np.median(np.abs(np.diff(lat)))) if lat.size > 1 else 1.0
    lead = da[lead_dim].values
    hours = (np.diff(lead) / np.timedelta64(1, "h")).astype(float)
    # Search on sign * field for maxima, so a low is a high of -field.
    sign = -1.0 if kind == "low" else 1.0
    k = max(1, round(smooth / cell))

    n_member, n_lead = da.sizes["number"], da.sizes[lead_dim]
    track_lat = np.full((n_member, n_lead), np.nan)
    track_lon = np.full((n_member, n_lead), np.nan)
    value = np.full((n_member, n_lead), np.nan)
    depth = np.full((n_member, n_lead), np.nan)
    for m in range(n_member):
        pos, radius = (start_lat, start_lon), search
        for t in range(n_lead):
            raw = da.values[m, t].astype(float)
            z = sign * raw
            # Box mean over the cells with data, half-width k cells, from 2-D
            # cumulative sums (one leading zero row and column, then k each side).
            finite = np.isfinite(z)
            sums = []
            for a in (np.where(finite, z, 0.0), finite.astype(float)):
                c = np.pad(a, ((k + 1, k), (k + 1, k))).cumsum(0).cumsum(1)
                w = 2 * k + 1
                sums.append(c[w:, w:] - c[:-w, w:] - c[w:, :-w] + c[:-w, :-w])
            total, n = sums
            smooth_z = np.where(n > 0, total / np.maximum(n, 1), np.nan)
            # Distance in degrees of latitude, longitude scaled by cos(lat).
            dist = np.hypot(glat - pos[0], (glon - pos[1]) * np.cos(np.deg2rad(pos[0])))
            window = np.where((dist <= radius) & np.isfinite(smooth_z), smooth_z, -np.inf)
            if not np.isfinite(window).any():
                break
            i, j = np.unravel_index(np.argmax(window), window.shape)
            centre = (lat[i], lon[j])
            ring_dist = np.hypot(
                glat - centre[0], (glon - centre[1]) * np.cos(np.deg2rad(centre[0]))
            )
            ring = smooth_z[(ring_dist >= RING_DEG[0]) & (ring_dist <= RING_DEG[1])]
            this_depth = smooth_z[i, j] - np.nanmean(ring) if ring.size else np.nan
            # A centre on the edge of its window is still heading out of it, and
            # one barely below its surroundings has filled: the track ends.
            if dist[i, j] > radius - cell or not this_depth >= min_depth:
                break
            track_lat[m, t], track_lon[m, t] = centre
            value[m, t], depth[m, t] = raw[i, j], this_depth
            pos = centre
            if t < n_lead - 1:
                # Never narrower than the smoothing box: at hourly leads a jittery
                # smoothed minimum would otherwise step off the window's edge.
                radius = max(max_move * hours[t] / 6.0, 2 * smooth, 2 * cell)

    # Per lead: the members' mean position and the circle holding --funnel % of them.
    need = min(n_member, max(5, int(np.ceil(min_members / 100 * n_member))))
    members = np.isfinite(track_lat).sum(axis=0)
    mean_lat = np.full(n_lead, np.nan)
    mean_lon = np.full(n_lead, np.nan)
    funnel_radius = np.full(n_lead, np.nan)
    circles = []
    for t in range(n_lead):
        ok = np.isfinite(track_lat[:, t])
        if ok.sum() < need:
            continue
        mean_lat[t], mean_lon[t] = track_lat[ok, t].mean(), track_lon[ok, t].mean()
        scale = np.cos(np.deg2rad(mean_lat[t]))
        dist = np.hypot(track_lat[ok, t] - mean_lat[t], (track_lon[ok, t] - mean_lon[t]) * scale)
        funnel_radius[t] = max(np.quantile(dist, funnel / 100), cell)
        unit = Point(0, 0).buffer(funnel_radius[t], 64).exterior.coords
        circles.append(Polygon([(mean_lon[t] + x / scale, mean_lat[t] + y) for x, y in unit]))
    if not circles:
        raise DataError(
            f"no lead had {need} tracked members; check --start, or loosen --search / --min-depth"
        )

    out = xr.Dataset(
        {
            "track_latitude": (("number", lead_dim), track_lat),
            "track_longitude": (("number", lead_dim), track_lon),
            variable: (("number", lead_dim), value.astype(da.dtype)),
            "depth": (("number", lead_dim), depth.astype("float32")),
            "mean_latitude": ((lead_dim,), mean_lat),
            "mean_longitude": ((lead_dim,), mean_lon),
            "funnel_radius": ((lead_dim,), funnel_radius),
            "funnel_radius_km": ((lead_dim,), funnel_radius * KM_PER_DEG),
            "members": ((lead_dim,), members),
        },
        coords={c: da[c] for c in da.coords if not {lat_dim, lon_dim} & set(da[c].dims)},
    )
    out["track_latitude"].attrs.update(units="degrees_north", long_name=f"{kind} centre latitude")
    out["track_longitude"].attrs.update(units="degrees_east", long_name=f"{kind} centre longitude")
    out[variable].attrs = {
        **da.attrs,
        "long_name": f"{da.attrs.get('long_name', variable)} at the {kind} centre",
    }
    out["depth"].attrs.update(
        units=da.attrs.get("units", "1"),
        long_name=f"{kind} centre depth against the {RING_DEG[0]:g}-{RING_DEG[1]:g} degree ring mean",
    )
    out["mean_latitude"].attrs.update(
        units="degrees_north", long_name="ensemble-mean centre latitude"
    )
    out["mean_longitude"].attrs.update(
        units="degrees_east", long_name="ensemble-mean centre longitude"
    )
    out["funnel_radius"].attrs.update(
        units="degrees", long_name=f"radius holding {funnel:g}% of members"
    )
    out["funnel_radius_km"].attrs.update(
        units="km", long_name=f"radius holding {funnel:g}% of members"
    )
    out["members"].attrs.update(units="1", long_name="members still tracking the centre")
    out.attrs.update(
        featureType="trajectory",
        Conventions="CF-1.13",
        track_kind=kind,
        track_start=f"{start_lat:g}/{start_lon:g}",
        funnel_percent=funnel,
    )
    for t in range(n_lead):
        label = str(lead[t].astype("timedelta64[h]")) if lead_dim == "step" else str(lead[t])[:16]
        print(f"{label}: {members[t]} of {n_member} members", file=sys.stderr)

    if funnel_geojson:
        # Join each pair of consecutive circles by their hull, so the funnel narrows
        # and widens with the spread instead of bulging between leads.
        parts = [a.union(b).convex_hull for a, b in zip(circles, circles[1:], strict=False)]
        shape = unary_union(parts or circles)
        if isinstance(shape, Polygon):
            shape = MultiPolygon([shape])
        feature = {
            "type": "Feature",
            "properties": {"kind": kind, "funnel_percent": funnel, "variable": variable},
            "geometry": mapping(shape),
        }
        try:
            Path(funnel_geojson).write_text(
                json.dumps({"type": "FeatureCollection", "features": [feature]})
            )
        except OSError as exc:
            raise DataError(f"could not write the funnel to {funnel_geojson}: {exc}") from None
        print(f"Wrote funnel polygon: {funnel_geojson}", file=sys.stderr)
    return out


if __name__ == "__main__":
    track_centre()

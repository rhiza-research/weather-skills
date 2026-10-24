# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "shapely",
# ]
# ///
"""Add label coords to a Zarr: the region each cell or station lies in, or a time group."""

import json
import sys
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import shape
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.region import LEVELS, level_features
from weather_skills_core.standard_dataset import detect_spatial_dims, names_for

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_TIME_PARTS = ("month", "year", "dayofyear")
_INITIALS = "JFMAMJJASOND"
# Stations outside every outline join the nearest unit this close (degrees, ~11 km): the
# bundled 1:50m outlines cut corners, leaving coastal stations just offshore.
_SNAP_DEG = 0.1


@weather_skill(
    name="label-group",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--level",
    choices=LEVELS,
    help="Label each cell/station with the unit at this level containing it (mutex with --geojson).",
)
@weather_skill.argument(
    "--geojson", help="GeoJSON FeatureCollection to label by (mutex with --level)."
)
@weather_skill.argument("--property", help="With --geojson: feature property holding the label.")
@weather_skill.argument(
    "--time",
    action="append",
    help="month, year, dayofyear, or month groups like MAM,OND (repeatable; groups add up).",
)
def label_group(ds, level, geojson, property, time, **kwargs):
    """Add label coords to a Zarr: the region each cell or station lies in, or a time group."""
    if level is not None and geojson is not None:
        raise UsageError("pass --level or --geojson, not both")
    if (geojson is None) != (property is None):
        raise UsageError("--geojson and --property go together")
    if level is None and geojson is None and not time:
        raise UsageError("pass at least one of --level, --geojson, --time")

    if level is not None or geojson is not None:
        # Grid (lat/lon dims) or stations (1-D lat/lon coords on one station dim).
        try:
            lat_dim, lon_dim = detect_spatial_dims(ds)
        except UsageError:
            # point-value output carries grid_latitude/grid_longitude too, which CF detection
            # cannot tell apart from the station coords.
            lat_dim = next((n for n in names_for("lat") if n in ds.coords), None)
            lon_dim = next((n for n in names_for("lon") if n in ds.coords), None)
            if lat_dim is None or lon_dim is None:
                raise
        if lat_dim in ds.dims and lon_dim in ds.dims:
            space_dims = (lat_dim, lon_dim)
            lon2d, lat2d = np.meshgrid(ds[lon_dim].values, ds[lat_dim].values)
        elif ds[lat_dim].ndim == 1 and ds[lat_dim].dims == ds[lon_dim].dims:
            space_dims = ds[lat_dim].dims
            lon2d, lat2d = np.asarray(ds[lon_dim].values), np.asarray(ds[lat_dim].values)
        else:
            raise UsageError("--level / --geojson need latitude/longitude dims or station coords")
        if np.nanmax(lon2d, initial=-np.inf) > 180.0:
            raise UsageError(
                f"longitudes run to {np.nanmax(lon2d):g}; label-group needs -180..180 "
                "(the outlines' range)"
            )

        stations = space_dims != (lat_dim, lon_dim)

        if level is not None:
            south, north = float(np.nanmin(lat2d)), float(np.nanmax(lat2d))
            lon360 = lon2d % 360.0
            if np.nanmax(lon360) - np.nanmin(lon360) < np.nanmax(lon2d) - np.nanmin(lon2d):
                # Crosses 180°: a box with west > east, which level_features reads as two spans.
                west, east = float(np.nanmin(lon360)), float(np.nanmax(lon360)) - 360.0
            else:
                west, east = float(np.nanmin(lon2d)), float(np.nanmax(lon2d))
            # Pad the box: admin layers are picked by the bundled country outlines, which
            # can stop a few km short of geoBoundaries' (Madagascar's north tip), and
            # stations snap to units this close.
            south, north = south - _SNAP_DEG, north + _SNAP_DEG
            west, east = west - _SNAP_DEG, east + _SNAP_DEG
            if west < -180.0 and east > 180.0:
                west, east = -180.0, 180.0
            elif west < -180.0:
                west += 360.0
            elif east > 180.0:
                east -= 360.0
            features = level_features(level, (north, west, south, east))
            coord = level
            labels = [f["properties"]["region_name"] for f in features]
            names = [f["properties"]["name"] for f in features]
        else:
            try:
                payload = json.loads(Path(geojson).read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise UsageError(f"could not read --geojson {geojson}: {exc}") from None
            features = payload.get("features") if isinstance(payload, dict) else None
            if not features:
                raise UsageError(f"--geojson {geojson} is not a FeatureCollection with features")
            missing = [
                i
                for i, f in enumerate(features)
                if (f.get("properties") or {}).get(property) is None
            ]
            if missing:
                raise UsageError(
                    f"--property {property!r} missing or null on {len(missing)} feature(s) of {geojson}"
                )
            coord = property
            labels = [str(f["properties"][property]) for f in features]
            names = None
        for name in (coord, f"{coord}_name") if names is not None else (coord,):
            if name in ds.variables:
                raise UsageError(f"{name!r} already exists in the dataset")

        # Features with a null geometry contain nothing.
        geoms = [shape(f["geometry"]) if f.get("geometry") else None for f in features]
        if any(g is not None and g.bounds[2] > 180.0 for g in geoms):
            raise UsageError(f"--geojson {geojson} has longitudes above 180; use -180..180")

        # Each cell centre / station takes the first unit that contains it, border included.
        out = np.full(lat2d.shape, "", dtype=object)
        out_names = np.full(lat2d.shape, "", dtype=object)
        empty = []
        contested = {}
        for i, geom in enumerate(geoms):
            inside = np.zeros(lat2d.shape, dtype=bool)
            if geom is not None:
                west, south, east, north = geom.bounds
                near = (lon2d >= west) & (lon2d <= east) & (lat2d >= south) & (lat2d <= north)
                inside[near] = shapely.intersects_xy(geom, lon2d[near], lat2d[near])
            if not inside.any():
                empty.append(labels[i])
            # Overlapping outlines (neighbouring countries' admin layers): first unit keeps it.
            for other in np.unique(out[inside & (out != "")]):
                pair = f"{other} / {labels[i]}"
                contested[pair] = contested.get(pair, 0) + int((inside & (out == other)).sum())
            inside &= out == ""
            out[inside] = labels[i]
            if names is not None:
                out_names[inside] = names[i]
        if stations:
            # A station in no unit joins the nearest one within _SNAP_DEG; ties keep the first.
            lost = np.nonzero((out == "") & np.isfinite(lon2d) & np.isfinite(lat2d))[0]
            points = shapely.points(lon2d[lost], lat2d[lost])
            best = np.full(lost.shape, np.inf)
            nearest = np.zeros(lost.shape, dtype=int)
            for i, geom in enumerate(geoms if lost.size else []):
                if geom is None:
                    continue
                distance = shapely.distance(geom, points)
                nearest = np.where(distance < best, i, nearest)
                best = np.minimum(distance, best)
            snap = best <= _SNAP_DEG
            out[lost[snap]] = [labels[i] for i in nearest[snap]]
            if names is not None:
                out_names[lost[snap]] = [names[i] for i in nearest[snap]]
            if snap.any():
                print(
                    f"{int(snap.sum())} station(s) outside every unit joined the nearest unit "
                    f"within {_SNAP_DEG}° (~11 km)",
                    file=sys.stderr,
                )
            empty = [label for label in empty if label not in set(out)]
        if empty:
            shown = ", ".join(empty[:10]) + (" ..." if len(empty) > 10 else "")
            print(
                f"{len(empty)} unit(s) contain no cell centre or station: {shown}",
                file=sys.stderr,
            )
        if contested:
            shown = ", ".join(f"{pair} ({n})" for pair, n in list(contested.items())[:10])
            print(
                f"{sum(contested.values())} cell(s)/station(s) lie in two units (overlap or "
                f"shared border) and keep the first: {shown}",
                file=sys.stderr,
            )
        print(f"{int((out == '').sum())} cell(s)/station(s) in no unit", file=sys.stderr)

        ds = ds.assign_coords({coord: (space_dims, out.astype(str))})
        ds[coord].attrs["long_name"] = (
            f"{level} unit containing the point"
            if level
            else f"GeoJSON {property} containing the point"
        )
        if names is not None:
            ds = ds.assign_coords({f"{coord}_name": (space_dims, out_names.astype(str))})

    if time:
        if "time" not in ds.dims:
            raise UsageError(
                "--time needs a time dimension; relabel forecasts by valid date first (step-to-time)"
            )
        tokens = [t.strip() for spec in time for t in spec.split(",") if t.strip()]
        parts = list(dict.fromkeys(t for t in tokens if t in _TIME_PARTS))
        groups = list(dict.fromkeys(t for t in tokens if t not in _TIME_PARTS))
        month = ds["time"].dt.month.values
        group_label = np.full(month.shape, "", dtype=object)
        # Months of a group that crosses December before its first month.
        begins_last_year = np.zeros(month.shape, dtype=bool)
        taken = set()
        for token in groups:
            # A run of month initials (MAM, DJF) matches exactly one start month.
            starts = [
                s
                for s in range(12)
                if len(token) <= 12 and (_INITIALS * 2)[s : s + len(token)] == token
            ]
            if len(starts) != 1:
                why = "matches more than one run of months" if starts else "is not a run of months"
                raise UsageError(
                    f"--time {token!r} {why}; pass month, year, dayofyear, or uppercase month "
                    "initials like MAM,OND or DJF"
                )
            months = [(starts[0] + i) % 12 + 1 for i in range(len(token))]
            if taken & set(months):
                raise UsageError(f"--time month groups share a month ({','.join(groups)})")
            taken |= set(months)
            in_group = np.isin(month, months)
            group_label[in_group] = token
            begins_last_year |= in_group & (month < months[0])

        for coord in parts + (["month_group"] if groups else []):
            if coord in ds.variables:
                raise UsageError(f"{coord!r} already exists in the dataset")
            if coord == "year":
                # A group crossing December takes the year it begins (DJF 2023 = Dec 2023-Feb 2024).
                values = ds["time"].dt.year.values - begins_last_year
            elif coord == "month_group":
                values = group_label.astype(str)
            else:
                values = getattr(ds["time"].dt, coord).values
            ds = ds.assign_coords({coord: ("time", values)})
    return ds


if __name__ == "__main__":
    label_group()

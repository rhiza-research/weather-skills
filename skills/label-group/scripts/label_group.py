# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/weather-skills/weather-skills-core@dev",  # region.level_features is not on PyPI yet
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
_SKILL_VERSION = "0.0.1"

_TIME_PARTS = ("month", "year", "dayofyear")
_INITIALS = "JFMAMJJASOND"


def _month_group(token):
    """Months (1-12) of a run of month initials such as MAM or DJF."""
    hits = [
        start
        for start in range(12)
        if len(token) <= 12 and all(_INITIALS[(start + i) % 12] == c for i, c in enumerate(token))
    ]
    if not token or len(hits) != 1:
        why = (
            "matches more than one run of months"
            if len(hits) > 1 and token
            else "is not a run of months"
        )
        raise UsageError(
            f"--time {token!r} {why}; pass month, year, dayofyear, or uppercase month "
            "initials like MAM,OND or DJF"
        )
    return [(hits[0] + i) % 12 + 1 for i in range(len(token))]


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
    help="month, year, dayofyear, or month groups like MAM,OND (repeatable).",
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
        lon2d = (lon2d + 180.0) % 360.0 - 180.0  # match against [-180, 180) outlines

        if level is not None:
            bbox = (
                float(np.nanmax(lat2d)),
                float(np.nanmin(lon2d)),
                float(np.nanmin(lat2d)),
                float(np.nanmax(lon2d)),
            )
            features = level_features(level, bbox)
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
                i for i, f in enumerate(features) if property not in (f.get("properties") or {})
            ]
            if missing:
                raise UsageError(
                    f"--property {property!r} missing on {len(missing)} feature(s) of {geojson}"
                )
            coord = property
            labels = [str(f["properties"][property]) for f in features]
            names = None
        if coord in ds.variables:
            raise UsageError(f"{coord!r} already exists in the dataset")

        # Each cell centre / station takes the first unit that contains it.
        out = np.full(lat2d.shape, "", dtype=object)
        out_names = np.full(lat2d.shape, "", dtype=object)
        empty = []
        contested = {}
        for i, feature in enumerate(features):
            geom = shape(feature["geometry"])
            west, south, east, north = geom.bounds
            near = (lon2d >= west) & (lon2d <= east) & (lat2d >= south) & (lat2d <= north)
            inside = np.zeros(lat2d.shape, dtype=bool)
            inside[near] = shapely.contains_xy(geom, lon2d[near], lat2d[near])
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
        if empty:
            shown = ", ".join(empty[:10]) + (" ..." if len(empty) > 10 else "")
            print(
                f"{len(empty)} unit(s) contain no cell centre or station: {shown}",
                file=sys.stderr,
            )
        if contested:
            shown = ", ".join(f"{pair} ({n})" for pair, n in list(contested.items())[:10])
            print(
                f"{sum(contested.values())} cell(s)/station(s) lie in two overlapping units and "
                f"keep the first: {shown}",
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
        parts = list(dict.fromkeys(t.strip() for t in time))
        group_specs = [p for p in parts if p not in _TIME_PARTS]
        if len(group_specs) > 1:
            raise UsageError("pass one --time month-group list, e.g. --time MAM,OND")
        month = ds["time"].dt.month.values
        group_label = np.full(month.shape, "", dtype=object)
        # Month -> first month of its group's year, for groups that cross December.
        begins_last_year = np.zeros(month.shape, dtype=bool)
        if group_specs:
            taken = set()
            for token in group_specs[0].split(","):
                token = token.strip()
                months = _month_group(token)
                if taken & set(months):
                    raise UsageError(f"--time month groups share a month ({group_specs[0]})")
                taken |= set(months)
                in_group = np.isin(month, months)
                group_label[in_group] = token
                begins_last_year |= in_group & (month < months[0])

        for part in parts:
            coord = "month_group" if part in group_specs else part
            if coord in ds.variables:
                raise UsageError(f"{coord!r} already exists in the dataset")
            if part == "year":
                # A group crossing December takes the year it begins (DJF 2023 = Dec 2023-Feb 2024).
                values = ds["time"].dt.year.values - begins_last_year
            elif part in _TIME_PARTS:
                values = getattr(ds["time"].dt, part).values
            else:
                values = group_label.astype(str)
            ds = ds.assign_coords({coord: ("time", values)})
    return ds


if __name__ == "__main__":
    label_group()

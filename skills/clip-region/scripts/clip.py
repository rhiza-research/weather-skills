# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "shapely",
# ]
# ///
"""Clip a gridded or station Zarr to a lat/lon box, a named region, or a GeoJSON polygon."""

from shapely.geometry import shape
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.region import lookup_region
from weather_skills_core.standard_utils import bbox_subset, clip_by_geometry, polygon_from_geojson

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"


@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset(["spatial", ("point_id",)]), required=True)
@weather_skill.argument(
    "--bbox",
    help="N/W/S/E; keeps cells whose centre is inside (mutex with --region and --geojson).",
)
@weather_skill.argument(
    "--region",
    help="ISO3, country, named region, or country-admin1 (mutex with --bbox and --geojson).",
)
@weather_skill.argument(
    "--geojson",
    help="GeoJSON polygon path (mutex with --bbox and --region).",
)
@weather_skill.argument(
    "--keep-outside",
    action="store_true",
    help="With --geojson or --region: NaN outside instead of dropping cells/stations.",
)
def clip_region(ds, bbox, region, geojson, keep_outside, **kwargs):
    """Clip a gridded or station Zarr to a lat/lon box, a named region, or a GeoJSON polygon."""
    if keep_outside and geojson is None and region is None:
        raise UsageError("--keep-outside requires --geojson or --region")
    n_set = sum(value is not None for value in (bbox, geojson, region))
    if n_set != 1:
        raise UsageError("exactly one of --bbox, --geojson, or --region is required")
    if region is not None:
        feature = lookup_region(region)
        geometry = feature.get("geometry")
        if geometry is None:
            raise UsageError(f"--region {region!r} has no polygon geometry")
        return clip_by_geometry(ds, shape(geometry), drop=not keep_outside)
    if geojson is not None:
        return clip_by_geometry(
            ds,
            polygon_from_geojson(geojson, flag="--geojson"),
            drop=not keep_outside,
        )
    return bbox_subset(ds, bbox)


if __name__ == "__main__":
    clip_region()

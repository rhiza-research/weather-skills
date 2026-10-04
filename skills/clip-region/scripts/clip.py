# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "shapely",
# ]
# ///
"""Subset by bbox, named region, or GeoJSON polygon."""

import sys

from shapely.geometry import shape
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.region import lookup_region
from weather_skills_core.standard_utils import (
    bbox_subset,
    clip_by_geometry,
    parse_bbox,
    polygon_from_geojson,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _to_pm180(lon: float) -> float:
    """Map a longitude written in 0..360 notation onto [-180, 180]."""
    return lon - 360.0 if lon > 180.0 else lon


def _resolve_bbox(bbox) -> tuple:
    """Take ``(N, W, S, E)`` (or an ``N/W/S/E`` string); decide if west > east wraps.

    The data are always clipped on a [-180, 180] axis (0..360 grids are
    wrapped first), so the box is mapped there too. West > east is read as a
    wrap (the box runs east from ``W`` across a seam to ``E``) only when that
    wrapped span is at most 180°: then the box crosses the antimeridian or,
    written in 0..360 notation, the 0/360 meridian. A wider wrap is far more
    likely W/E swapped — silently honouring it returns the complement of the
    intended box — so it is refused.
    """
    # The decorator hands --bbox over already parsed to a float tuple.
    north, west, south, east = parse_bbox(bbox) if isinstance(bbox, str) else bbox
    bbox = f"{north:g}/{west:g}/{south:g}/{east:g}"
    for name, lon in (("west", west), ("east", east)):
        if not -180.0 <= lon <= 360.0:
            raise UsageError(f"--bbox {name} longitude {lon} is outside [-180, 360].")
    w, e = _to_pm180(west), _to_pm180(east)
    if west > east:
        span = (east - west) % 360.0
        if span > 180.0:
            raise UsageError(
                f"--bbox {bbox}: west {west} > east {east}. Read as a box running "
                f"east across a seam it would be {span:g}° wide — the complement of "
                f"the {360.0 - span:g}° box between them — so W/E look swapped. "
                f"Use N/W/S/E, e.g. {north:g}/{east:g}/{south:g}/{west:g}. A box that really "
                "crosses the antimeridian has west > 0 > east (e.g. 10/170/-10/-170); "
                "a wider one can be written in 0..360 (e.g. 10/100/-10/300)."
            )
    if w > e:
        print(
            f"clip-region: note: --bbox crosses the antimeridian; selecting "
            f"{w:g}..180 and -180..{e:g} ({(e - w) % 360.0:g}° wide).",
            file=sys.stderr,
        )
    elif west > east:
        print(
            f"clip-region: note: --bbox crosses the 0/360 meridian; selecting "
            f"{w:g}..{e:g} on the [-180, 180] axis.",
            file=sys.stderr,
        )
    return north, w, south, e


@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset(["spatial", "point_obs"]), required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--region",
    default=None,
    help=(
        "ISO3, country, or named region. Clips to the boundary polygon and "
        "keeps every grid cell that overlaps it, including cells that "
        "straddle the border. Mutex with --bbox and --geojson."
    ),
)
@weather_skill.argument(
    "--geojson",
    default=None,
    help="GeoJSON polygon path (mutex with --bbox and --region).",
)
@weather_skill.argument(
    "--keep-outside",
    action="store_true",
    help="With --geojson or --region: NaN outside instead of dropping cells/stations.",
)
def clip_region(ds, output, bbox=None, geojson=None, region=None, keep_outside=False, **kwargs):
    """Subset by bbox, named region, or GeoJSON polygon."""
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
    return bbox_subset(ds, _resolve_bbox(bbox))


if __name__ == "__main__":
    clip_region()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "shapely>=2.1",
# ]
# ///
"""Turn a country, region, or place name into a lat/lon bounding box and optional boundary GeoJSON."""

import json
import sys
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.region import (
    bbox_from_feature,
    geocode_nominatim,
    lookup_region,
    should_geocode,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

# OSM feature types that name an area but come back from Nominatim as one centre
# point ("Horn of Africa" is natural/peninsula). Padding that point would print a
# confident-looking box that is not the area asked for, so these are refused.
_AREA_TYPES = frozenset(
    {
        "archipelago",
        "bay",
        "continent",
        "country",
        "desert",
        "mountain_range",
        "ocean",
        "peninsula",
        "plateau",
        "region",
        "sea",
        "state",
        "strait",
        "valley",
    }
)


@weather_skill(
    name="resolve-region",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "code",
    help=(
        "ISO3 country code (uppercase, e.g. KEN), named region (East Africa), "
        "custom box (Indian Ocean basin), "
        "sub-national region (kenya-nairobi), or leftover place name "
        "(Mount Kenya, Kenya)"
    ),
)
@weather_skill.argument(
    "--geojson",
    help="Optional path: write the boundary polygon as GeoJSON",
)
@weather_skill.argument(
    "--pad",
    type=float,
    default=0.25,
    help=(
        "Degrees added on every side of a point-sized landmark (a peak, a town "
        "node, a station) so it becomes an area (default 0.25)."
    ),
)
def resolve_region(code, geojson, pad, **kwargs):
    """Turn a country, region, or place name into a lat/lon bounding box and optional boundary GeoJSON."""
    text = code.strip()
    if not text:
        raise UsageError(
            "pass an ISO3 code (e.g. KEN), a named region (e.g. East Africa), "
            "a custom box (e.g. 'Indian Ocean'), "
            "a sub-national region (e.g. kenya-nairobi), "
            "or a landmark (e.g. 'Nairobi, Kenya')."
        )
    if len(text) == 3 and text.isalpha() and text != text.upper():
        raise UsageError(
            f"{code!r} is not an ISO 3166-1 alpha-3 (iso3) code. "
            "Pass a three-letter uppercase code (e.g. KEN), not a name or alpha-2 code."
        )

    try:
        match = lookup_region(code)
    except DataError:
        if not should_geocode(code):
            raise
        match = geocode_nominatim(code)
        display = match["properties"]["display_name"]
        print(f"nominatim: {display}", file=sys.stderr)
    n, w, s, e = bbox_from_feature(match)
    if match["properties"].get("level") == "nominatim":
        category = match["properties"].get("osm_category")
        kind = match["properties"].get("osm_type")
        if category == "highway":
            # "western Indian Ocean" first matches Indian Ocean Drive in Western Australia.
            raise DataError(
                f"Nominatim matched {display!r}, a road ({category}/{kind}), not a region. "
                "Pass an ISO3 code, country-admin1, a named region, or an explicit "
                "N/W/S/E bbox."
            )
        if abs(n - s) < 0.05 and abs(e - w) < 0.05:
            if kind is None or kind in _AREA_TYPES:
                raise DataError(
                    f"Nominatim matched {display!r} ({category}/{kind}), but OSM has only a "
                    f"centre point for it ({(n + s) / 2:.3f}, {(w + e) / 2:.3f}), not its area. "
                    "Pass an explicit N/W/S/E bbox for the region you mean."
                )
            # A genuine point feature: pad it into an area and say so.
            n, w, s, e = (round(v, 4) for v in (n + pad, w - pad, s - pad, e + pad))
            print(
                f"point feature ({category}/{kind}); padded by {pad:g} deg on each side "
                "(--pad to change)",
                file=sys.stderr,
            )
            match = {
                **match,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
                },
            }

    # Write the polygon (guarded) BEFORE printing the bbox, so a failed write
    # never emits a valid-looking bbox to stdout that a caller might consume.
    if geojson:
        out_fc = {"type": "FeatureCollection", "features": [match]}
        try:
            Path(geojson).write_text(json.dumps(out_fc, separators=(",", ":")))
        except OSError as exc:
            raise DataError(f"could not write boundary polygon to {geojson}: {exc}") from None
        print(f"Wrote boundary polygon: {geojson}", file=sys.stderr)

    print(f"{n}/{w}/{s}/{e}")


if __name__ == "__main__":
    resolve_region()

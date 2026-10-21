# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "pandas",
#   "jaxa-earth",
# ]
# ///
"""Fetch JAXA GSMaP hourly precipitation as a weather-skills Zarr."""

import sys

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# JAXA's own Earth API (data.earth.jaxa.jp / pip install jaxa-earth), not
# Earth Engine: registration-free, no credentials, genuinely bbox-subsetted
# server-side (COG + STAC -- confirmed by the returned array being
# region-sized, not global, and by the tiepoint-based tiling code itself).
_COLLECTION = "JAXA.EORC_GSMaP_standard.Gauge.v6_hourly"
_BAND = "PRECIP"
_PPU = 10  # pixels per degree = 0.1 deg, GSMaP's native resolution


def _fetch_day(day_str: str, bbox):
    """One UTC day of hourly GSMaP for bbox, plus each image's UTC hour.

    Returns ``(None, [])`` for a day JAXA has not published (or never had).

    The date filter is day-granular -- a sub-day window raises inside the
    package ("No date list found!") -- so always request a full day.
    """
    import pandas as pd
    from jaxa.earth import je

    n, w, s, e = bbox
    dlim = [f"{day_str}T00:00:00", f"{day_str}T23:59:59"]
    try:
        coll = je.ImageCollection(collection=_COLLECTION).filter_date(dlim=dlim)
    except Exception as exc:  # jaxa-earth raises a bare Exception for a day with no images
        if "No date list found" not in str(exc):
            raise
        return None, []
    # STAC ids ("YYYY-MM/DD-hh/") label each returned image's UTC hour.
    hours = [pd.Timestamp(f"{i[:4]}-{i[5:7]}-{i[8:10]}T{i[11:13]}:00") for i in coll.stac_date.id]
    coll = coll.filter_resolution(ppu=_PPU).filter_bounds(bbox=[w, s, e, n]).select(band=_BAND)
    # jaxa-earth NaNs every pixel, in every image, outside the FIRST image's
    # stored [min, max] -- that image's own value range (e.g. max 54 mm/hr),
    # not a validity range -- so heavier rain in any later hour or the other
    # hemisphere tile became NaN. Drop that range; _to_dataarray masks the
    # negative error codes itself.
    coll.stac_bounds.json[0][0]["assets"][_BAND]["je:rasters"]["dn"].pop("max", None)
    return coll.get_images().raster, hours


def _to_dataarray(raster, hours):
    """raster.img is (hour, lat, lon, band), one image per entry of ``hours``."""
    import numpy as np
    import xarray as xr

    img = np.asarray(raster.img)[..., 0]  # drop the trailing singleton band axis
    # Negative values are JAXA error codes (-4, -8, -99; -999.9 nodata), not rain.
    img = np.where(img < 0, np.nan, img)
    n_hours, n_lat, n_lon = img.shape
    if n_hours != len(hours):
        raise DataError(f"got {n_hours} images for {len(hours)} listed hours.")

    (south, north), (west, east) = raster.latlim[0], raster.lonlim[0]
    # Standard GeoTIFF/raster convention: row 0 is the northernmost row
    # (tiepoint anchors the top-left/north-west corner) -- descending lat.
    lat = np.linspace(north, south, n_lat, endpoint=False) - (north - south) / n_lat / 2
    lon = np.linspace(west, east, n_lon, endpoint=False) + (east - west) / n_lon / 2

    return xr.DataArray(
        img,
        dims=("time", "latitude", "longitude"),
        coords={"time": hours, "latitude": lat, "longitude": lon},
    )


@weather_skill(name="gsmap-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--bbox",
    required=True,
    help=(
        "N/W/S/E bounding box (required -- genuinely subsets server-side; "
        "there is no whole-globe fetch here). Use resolve-region to turn a "
        "country or named region into this value."
    ),
)
def fetch(start_time, end_time, bbox, **kwargs):
    """Fetch JAXA GSMaP hourly precipitation as a weather-skills Zarr."""
    import pandas as pd
    import xarray as xr

    n, w, s, e = bbox
    if n > 60 or s < -60:
        raise UsageError(f"--bbox latitude {n:g}..{s:g} is outside GSMaP's 60N-60S coverage.")
    # JAXA serves -180..180 longitudes; accept 0..360 values too.
    w = w - 360 if w >= 180 else w
    e = e - 360 if e > 180 else e
    if w >= e:
        raise UsageError(
            f"--bbox west {w:g} must be less than east {e:g}; "
            "split an antimeridian-crossing box into two fetches and concat them."
        )
    bbox = (n, w, s, e)

    print(
        f"gsmap-fetch: fetching {_COLLECTION} {start_time} -> {end_time} "
        f"bbox={bbox[0]:g}/{bbox[1]:g}/{bbox[2]:g}/{bbox[3]:g}",
        file=sys.stderr,
    )

    days = pd.date_range(start_time.isoformat(), end_time.isoformat(), freq="D")
    pieces = []
    for day in days:
        day_str = day.strftime("%Y-%m-%d")
        raster, hours = _fetch_day(day_str, bbox)
        if not hours:
            print(f"gsmap-fetch: JAXA serves no images for {day_str}; skipped.", file=sys.stderr)
            continue
        pieces.append(_to_dataarray(raster, hours))
    if not pieces:
        raise DataError(
            f"JAXA serves no GSMaP images for {start_time} -> {end_time} "
            "(the archive starts April 2025; recent days are published a few days late)."
        )

    precip = xr.concat(pieces, dim="time").rename("precip")
    print(
        f"gsmap-fetch: {precip.sizes['time']} of {24 * len(days)} hours served; "
        "missing hours are absent rows, not filled.",
        file=sys.stderr,
    )
    precip.attrs = {"units": "mm/hr"}
    out = precip.to_dataset().load()

    out = to_standard_units(out, variables=["precip"])
    out.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"jaxa-earth-api:{_COLLECTION}",
    )
    stamp_cf_attrs(out)
    return stamp_data_interval(out, period="1 hour")


if __name__ == "__main__":
    fetch()

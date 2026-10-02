# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "pint-xarray>=0.6",
#   "jaxa-earth",
# ]
# ///
"""Fetch GSMaP hourly precipitation directly from JAXA (credential-free) and write a weather-skills standard dataset Zarr."""

import sys

from weather_skills_core import DataError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# JAXA's own Earth API (data.earth.jaxa.jp / pip install jaxa-earth), not
# Earth Engine: registration-free, no credentials, genuinely bbox-subsetted
# server-side (COG + STAC -- confirmed by the returned array being
# region-sized, not global, and by the tiepoint-based tiling code itself).
_COLLECTION = "JAXA.EORC_GSMaP_standard.Gauge.v6_hourly"
_BAND = "PRECIP"
_PPU = 10  # pixels per degree = 0.1 deg, GSMaP's native resolution


def _fetch_day(day_str: str, bbox):
    """One UTC day of hourly GSMaP for bbox. The date filter is day-granular
    -- a sub-day window raises inside the package ("No date list found!") --
    so always request a full day, never an hour-level range."""
    from jaxa.earth import je

    n, w, s, e = bbox
    dlim = [f"{day_str}T00:00:00", f"{day_str}T23:59:59"]
    coll = je.ImageCollection(collection=_COLLECTION)
    data = (
        coll.filter_date(dlim=dlim)
        .filter_resolution(ppu=_PPU)
        .filter_bounds(bbox=[w, s, e, n])
        .select(band=_BAND)
        .get_images()
    )
    return data.raster


def _to_dataarray(raster, day_str: str):
    """raster.img is (hour, lat, lon, band); no time labels are returned
    directly, so hours are reconstructed assuming a full 0-23 UTC sequence
    with any missing/failed hours dropped from the front -- verified against
    the array's own hour count, not assumed blindly."""
    import numpy as np
    import pandas as pd
    import xarray as xr

    img = np.asarray(raster.img)[..., 0]  # drop the trailing singleton band axis
    n_hours, n_lat, n_lon = img.shape

    (south, north), (west, east) = raster.latlim[0], raster.lonlim[0]
    # Standard GeoTIFF/raster convention: row 0 is the northernmost row
    # (tiepoint anchors the top-left/north-west corner) -- descending lat.
    lat = np.linspace(north, south, n_lat, endpoint=False) - (north - south) / n_lat / 2
    lon = np.linspace(west, east, n_lon, endpoint=False) + (east - west) / n_lon / 2

    all_hours = pd.date_range(
        f"{day_str}T00:00:00", periods=24, freq="h"
    )  # naive UTC, not tz-aware
    if n_hours > 24:
        raise DataError(f"got {n_hours} hourly images for one day; expected <= 24.")
    time = all_hours[-n_hours:] if n_hours < 24 else all_hours

    da = xr.DataArray(
        img,
        dims=("time", "latitude", "longitude"),
        coords={"time": time, "latitude": lat, "longitude": lon},
    )
    return da


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
    """Fetch GSMaP hourly precipitation directly from JAXA (no credentials required)."""
    import pandas as pd
    import xarray as xr

    print(
        f"gsmap-fetch: fetching {_COLLECTION} {start_time} -> {end_time} "
        f"bbox={bbox[0]:g}/{bbox[1]:g}/{bbox[2]:g}/{bbox[3]:g}",
        file=sys.stderr,
    )

    days = pd.date_range(start_time.isoformat(), end_time.isoformat(), freq="D")
    pieces = []
    for day in days:
        day_str = day.strftime("%Y-%m-%d")
        raster = _fetch_day(day_str, bbox)
        pieces.append(_to_dataarray(raster, day_str))

    precip = xr.concat(pieces, dim="time").rename("precip")
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

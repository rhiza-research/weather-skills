# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core[dask] @ git+https://github.com/weather-skills/weather-skills-core@feat/dask-larger-than-memory",
#   "pandas",
#   "jaxa-earth",
# ]
# ///
"""Fetch JAXA GSMaP hourly precipitation as a weather-skills Zarr."""

import copy
import sys
from concurrent.futures import ThreadPoolExecutor

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

# JAXA's own Earth API (data.earth.jaxa.jp / pip install jaxa-earth), not
# Earth Engine: registration-free, no credentials, genuinely bbox-subsetted
# server-side (COG + STAC -- confirmed by the returned array being
# region-sized, not global, and by the tiepoint-based tiling code itself).
_COLLECTION = "JAXA.EORC_GSMaP_standard.Gauge.v6_hourly"
_BAND = "PRECIP"
_PPU = 10  # pixels per degree = 0.1 deg, GSMaP's native resolution
_WORKERS = 8  # concurrent STAC listings, when --dask reads the days lazily


def _list_day(day_str: str):
    """One UTC day's hourly images as a jaxa-earth collection, plus each image's UTC hour.

    Returns ``(None, [])`` for a day JAXA has not published (or never had).

    The date filter is day-granular -- a sub-day window raises inside the
    package ("No date list found!") -- so always request a full day.
    """
    import pandas as pd
    from jaxa.earth import je

    dlim = [f"{day_str}T00:00:00", f"{day_str}T23:59:59"]
    try:
        coll = je.ImageCollection(collection=_COLLECTION).filter_date(dlim=dlim)
    except Exception as exc:  # jaxa-earth raises a bare Exception for a day with no images
        if "No date list found" not in str(exc):
            raise
        return None, []
    # STAC ids ("YYYY-MM/DD-hh/") label each returned image's UTC hour.
    hours = [pd.Timestamp(f"{i[:4]}-{i[5:7]}-{i[8:10]}T{i[11:13]}:00") for i in coll.stac_date.id]
    return coll, hours


def _select_band(coll, bbox):
    """Point a listed day at ``bbox`` and the precip band (catalog lookups only, no image read)."""
    n, w, s, e = bbox
    coll = coll.filter_resolution(ppu=_PPU).filter_bounds(bbox=[w, s, e, n]).select(band=_BAND)
    # jaxa-earth NaNs every pixel, in every image, outside the FIRST image's
    # stored [min, max] -- that image's own value range (e.g. max 54 mm/hr),
    # not a validity range -- so heavier rain in any later hour or the other
    # hemisphere tile became NaN. Drop that range; _to_dataarray masks the
    # negative error codes itself.
    for per_tile in coll.stac_bounds.json:
        per_tile[0]["assets"][_BAND]["je:rasters"]["dn"].pop("max", None)
    return coll


def _read_day(coll, bbox):
    """Read a listed day's images over ``bbox``; returns jaxa-earth's raster."""
    return _select_band(coll, bbox).get_images().raster


def _read_hour(coll, i):
    """Read image ``i`` of a day prepared by ``_select_band``; returns jaxa-earth's raster.

    jaxa-earth reads (and holds, twice over) a whole day over the bbox in one call,
    which is gigabytes for a wide one. It reads image by image from per-image lists,
    so a shallow copy narrowed to one entry of each list reads just that hour.
    """
    hour = copy.copy(coll)
    for name, attrs in (
        ("stac_date", ("id",)),
        ("stac_band", ("url",)),
        ("stac_bounds", ("json", "lon_offsets")),
    ):
        narrowed = copy.copy(getattr(coll, name))
        for attr in attrs:
            setattr(narrowed, attr, [getattr(narrowed, attr)[i]])
        setattr(hour, name, narrowed)
    return hour.get_images().raster


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


def _lazy_days(days, bbox):
    """Hourly precipitation as dask arrays: one task per published hour, run at write time.

    The days are listed and pointed at ``bbox`` up front (STAC metadata only); the
    images are read by the tasks. Returns ``None`` when JAXA serves no day at all.
    """
    import dask
    import dask.array as da
    import numpy as np
    import pandas as pd
    import xarray as xr

    def prepare(day):
        coll, hours = _list_day(day.strftime("%Y-%m-%d"))
        return (_select_band(coll, bbox) if hours else None), hours

    with ThreadPoolExecutor(_WORKERS) as pool:
        listed = list(pool.map(prepare, days))
    for day, (_, hours) in zip(days, listed, strict=True):
        if not hours:
            print(
                f"gsmap-fetch: JAXA serves no images for {day:%Y-%m-%d}; skipped.", file=sys.stderr
            )
    images = [(coll, i, h) for coll, hours in listed for i, h in enumerate(hours)]
    if not images:
        return None
    # The first image gives the grid; every other one must sit on it.
    first = _to_dataarray(_read_hour(*images[0][:2]), [images[0][2]])

    def same_grid_hour(coll, i, hour):
        image = _to_dataarray(_read_hour(coll, i), [hour])
        for dim in ("latitude", "longitude"):
            if image[dim].shape != first[dim].shape or not np.allclose(image[dim], first[dim]):
                raise DataError(
                    f"the GSMaP image for {hour:%Y-%m-%d %H}:00 is not on the first one's grid."
                )
        return image.values

    arrays = [da.from_array(first.values, chunks=-1)] + [
        da.from_delayed(dask.delayed(same_grid_hour)(*image), first.shape, first.dtype)
        for image in images[1:]
    ]
    return xr.DataArray(
        da.concatenate(arrays),
        dims=first.dims,
        coords={
            "time": pd.DatetimeIndex([h for _, _, h in images]),
            "latitude": first["latitude"],
            "longitude": first["longitude"],
        },
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
def fetch(start_time, end_time, bbox, dask=False, **kwargs):
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
    if dask:
        precip = _lazy_days(days, bbox)
        pieces = [] if precip is None else [precip]
    else:
        pieces = []
        for day in days:
            day_str = day.strftime("%Y-%m-%d")
            coll, hours = _list_day(day_str)
            if not hours:
                print(
                    f"gsmap-fetch: JAXA serves no images for {day_str}; skipped.", file=sys.stderr
                )
                continue
            pieces.append(_to_dataarray(_read_day(coll, bbox), hours))
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
    out = precip.to_dataset()

    out = to_standard_units(out, variables=["precip"])
    out.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"jaxa-earth-api:{_COLLECTION}",
    )
    stamp_cf_attrs(out)
    return stamp_data_interval(out, period="1 hour")


if __name__ == "__main__":
    fetch()

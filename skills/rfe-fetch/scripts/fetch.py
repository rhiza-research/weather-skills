# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core[dask] @ git+https://github.com/weather-skills/weather-skills-core@feat/dask-larger-than-memory",
#   "rasterio",
# ]
# ///
"""Fetch NOAA CPC RFE2 daily rainfall over Africa as a weather-skills Zarr."""

import io
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

_BASE_URL = "https://ftp.cpc.ncep.noaa.gov/fews/fewsdata/africa/rfe2/geotiff/"
_START = date(2001, 1, 1)
_MISSING = -999.0  # RFE_readme.txt's missing-data value; the GeoTIFFs declare no nodata
_WORKERS = 8
_RETRIES = 3


def _url(day: date) -> str:
    return f"{_BASE_URL}africa_rfe.{day:%Y%m%d}.tif.zip"


def _read(url: str) -> bytes | None:
    """Body of ``url``, or None on 404 (a day CPC has not published or never had)."""
    for attempt in range(_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == _RETRIES - 1:
                raise DataError(f"CPC returned HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == _RETRIES - 1:
                raise DataError(f"could not reach CPC for {url}: {exc}") from exc
        time.sleep(2**attempt)


def _listed_days() -> list[date]:
    """Every day CPC lists in the GeoTIFF folder, oldest first."""
    listing = _read(_BASE_URL)
    stamps = sorted(set(re.findall(rb"africa_rfe\.(\d{8})\.tif\.zip", listing or b"")))
    if not stamps:
        raise DataError(f"no africa_rfe.YYYYMMDD.tif.zip files listed at {_BASE_URL}")
    return [date(int(s[:4]), int(s[4:6]), int(s[6:])) for s in map(bytes.decode, stamps)]


def _latest_day() -> date:
    return _listed_days()[-1]


def _decode(body: bytes, day: date, bbox):
    """One day's zipped GeoTIFF as a (time, latitude, longitude) DataArray cut to ``bbox``."""
    import numpy as np
    import rasterio
    import xarray as xr

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        tif = zf.read(f"africa_rfe.{day:%Y%m%d}.tif")
    with rasterio.MemoryFile(tif) as mem, mem.open() as src:
        grid = src.read(1).astype("float32")
        t = src.transform
    # Cell centres from the file's own geotransform. Its float32 origin and
    # spacing carry ~1e-8 noise, so round to 1e-4 degrees for clean labels.
    lat = np.round(t.f + t.e * (np.arange(grid.shape[0]) + 0.5), 4)
    lon = np.round(t.c + t.a * (np.arange(grid.shape[1]) + 0.5), 4)
    da = xr.DataArray(
        np.where(grid == _MISSING, np.nan, grid)[None],
        dims=("time", "latitude", "longitude"),
        coords={"time": [np.datetime64(day, "ns")], "latitude": lat, "longitude": lon},
    ).sortby("latitude")
    return bbox_subset(da, bbox) if bbox is not None else da


def _lazy_days(days, bbox, window):
    """Daily rainfall as dask arrays: one task per published day, run at write time.

    Returns ``(array, missing)``; days CPC does not list are absent rows.
    """
    import dask
    import dask.array as darr
    import numpy as np
    import xarray as xr

    listed = set(_listed_days())
    present = [d for d in days if d in listed]
    missing = [d for d in days if d not in listed]
    if not present:
        return None, missing

    def read(day):
        body = _read(_url(day))
        if body is None:
            raise DataError(f"CPC no longer serves {_url(day)}.")
        return _decode(body, day, bbox)

    # The first day gives the grid; every other day must sit on the same one.
    first = read(present[0])

    def same_grid_day(day):
        da = read(day)
        for dim in ("latitude", "longitude"):
            if not np.array_equal(da[dim], first[dim]):
                raise DataError(
                    f"RFE2 files in {window} are not on one grid: {day} differs from {present[0]}."
                )
        return da.values

    arrays = [darr.from_array(first.values, chunks=-1)] + [
        darr.from_delayed(dask.delayed(same_grid_day)(d), first.shape, first.dtype)
        for d in present[1:]
    ]
    precip = xr.DataArray(
        darr.concatenate(arrays),
        dims=first.dims,
        coords={
            "time": np.array(present, dtype="datetime64[ns]"),
            "latitude": first["latitude"],
            "longitude": first["longitude"],
        },
    )
    return precip, missing


@weather_skill(name="rfe-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--bbox",
    help=(
        "N/W/S/E bounding box inside 40N/20W/40S/55E. Omit for the whole RFE2 "
        "Africa grid. Use resolve-region to turn a country into this value."
    ),
)
@weather_skill.argument(
    "--probe-latest",
    action="store_true",
    probe=True,
    help="Print the latest published YYYY-MM-DD on stdout and exit.",
)
def fetch(start_time, end_time, bbox, probe_latest, dask=False, **kwargs):
    """Fetch NOAA CPC RFE2 daily rainfall over Africa as a weather-skills Zarr."""
    if probe_latest:
        print(_latest_day().isoformat())
        return

    import xarray as xr

    if start_time < _START:
        raise UsageError(f"--start-time {start_time} is before RFE2's first day {_START}.")

    days = [start_time + timedelta(d) for d in range((end_time - start_time).days + 1)]
    print(
        f"rfe-fetch: fetching {len(days)} day(s) {start_time} -> {end_time} from {_BASE_URL}",
        file=sys.stderr,
    )
    if dask:
        precip, missing = _lazy_days(days, bbox, f"{start_time} -> {end_time}")
        pieces = [] if precip is None else [precip]
    else:
        with ThreadPoolExecutor(_WORKERS) as pool:
            bodies = list(pool.map(_read, [_url(d) for d in days]))
        pieces, missing = [], []
        for day, body in zip(days, bodies, strict=True):
            if body is None:
                missing.append(day)
                continue
            # Subset each day as it arrives so a long window never holds the full grid.
            pieces.append(_decode(body, day, bbox))

    if not pieces:
        raise DataError(
            f"CPC has published no RFE2 day in {start_time} -> {end_time} "
            "(the archive starts 2001-01-01; each day appears about one day late)."
        )
    if missing:
        print(
            f"rfe-fetch: CPC has no file for {', '.join(d.isoformat() for d in missing)}; "
            "those days are absent rows, not filled.",
            file=sys.stderr,
        )

    try:
        precip = xr.concat(pieces, dim="time", join="exact").rename("precip")
    except ValueError as exc:
        raise DataError(
            f"RFE2 files in {start_time} -> {end_time} are not on one grid: {exc}"
        ) from exc
    precip.attrs = {"units": "mm day-1", "long_name": "NOAA CPC RFE2 daily rainfall estimate"}
    out = to_standard_units(precip.to_dataset(), variables=["precip"])
    out.attrs.update(Conventions="CF-1.13", weather_skills_source="noaa-cpc-rfe2")
    stamp_cf_attrs(out)
    return stamp_data_interval(out, period="1 day")


if __name__ == "__main__":
    fetch()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "earthaccess",
#   "h5py",
#   "pyproj",
# ]
# ///
"""Fetch NASA's intercalibrated SMAP/SMOS daily 9 km soil moisture (NSIDC-0800) as a weather-skills Zarr."""

# NSIDC-0800 holds, per day and overpass, SMAP's own retrieval and a SMOS one
# made with SMAP's algorithm from SMOS brightness temperatures calibrated to
# SMAP. Both sit on the EASE-Grid 2.0 global 9 km grid. A file is ~1.7 GB but
# uncompressed, so only the box's rows and columns of the needed fields are
# read over HTTPS; the grid's lat/lon are computed, not read.

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_SHORT_NAME = "NSIDC-0800"
_VERSION = "2"
_FIRST_DAY = date(2015, 3, 31)
_FILL = -9999.0
_VALID = (0.02, 0.5)  # m3 m-3, the product's valid_min / valid_max
_GRANULE_DATE_RE = re.compile(r"_(\d{8})_")
_GROUPS = {"smap": "SMAP_Based", "smos": "SMOS_Based"}
# Fill order; a value's soil_moisture_source is its 1-based place here.
_LAYERS = (("smap", "AM"), ("smos", "AM"), ("smap", "PM"), ("smos", "PM"))
_WORKERS = 4

# EASE-Grid 2.0 global 9 km (EPSG:6933): cell size and outer corner, in metres.
_ROWS, _COLS = 1624, 3856
_CELL = 9008.055210146
_X0, _Y0 = -17367530.445161499, 7314540.830638

_AUTH_FAIL_MSG = (
    "Earthdata authentication failed; configure EARTHDATA_USERNAME/"
    "EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) or a urs.earthdata.nasa.gov entry in "
    "~/.netrc, then retry."
)


def _grid():
    """1-D latitude (north to south) and longitude of the EASE-2 9 km cell centres."""
    import numpy as np
    from pyproj import Transformer

    to_ll = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True)
    x = _X0 + (np.arange(_COLS) + 0.5) * _CELL
    y = _Y0 - (np.arange(_ROWS) + 0.5) * _CELL
    lon, _ = to_ll.transform(x, np.zeros_like(x))
    _, lat = to_ll.transform(np.zeros_like(y), y)
    return lat, lon


def _login():
    import earthaccess

    for strategy in ("environment", "netrc"):
        try:
            if getattr(earthaccess.login(strategy=strategy), "authenticated", False):
                return
        except Exception:  # noqa: BLE001
            continue
    raise DataError(_AUTH_FAIL_MSG)


def _granule_date(granule):
    for url in granule.data_links():
        m = _GRANULE_DATE_RE.search(url.rsplit("/", 1)[-1])
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007
    raise ValueError("no YYYYMMDD date in the granule file name")


def _read(handle, source, overpass, rows, cols, recommended_only):
    """One source's soil moisture for the box, NaN where missing or (optionally) not recommended; None if the field is empty."""
    import numpy as np

    suffix = overpass.lower()
    grp = handle[_GROUPS[source]][f"Soil_Moisture_Retrieval_Data_{overpass}"]
    dset = grp[f"soil_moisture_{suffix}"]
    # Some granules are placeholders with nothing stored, which h5py reads as zeros.
    if dset.id.get_storage_size() == 0:
        return None
    sm = dset[rows, cols].astype("float32")
    lo, hi = float(dset.attrs.get("valid_min", _VALID[0])), float(dset.attrs.get("valid_max", _VALID[1]))
    bad = (sm == _FILL) | (sm < lo) | (sm > hi)
    if recommended_only:
        # Bit 0 set: retrieval not recommended.
        bad |= (grp[f"retrieval_qual_flag_{suffix}"][rows, cols] & 1) != 0
    return np.where(bad, np.nan, sm)


@weather_skill(name="smap-smos-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--overpass",
    choices=("AM", "PM", "both"),
    default="AM",
    help="AM = 6am, PM = 6pm local overpass; both = AM, gaps filled from PM. Default AM.",
)
@weather_skill.argument(
    "--source",
    choices=("merged", "smap", "smos"),
    default="merged",
    help="merged: SMAP, gaps filled from SMOS (default); smap or smos: one retrieval only.",
)
@weather_skill.argument(
    "--quality",
    choices=("recommended", "all"),
    default="recommended",
    help="recommended (default) keeps only retrievals flagged recommended; all keeps every one.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help="Print the last day NSIDC has (YYYY-MM-DD) and exit.",
)
def fetch(start_time, end_time, bbox, overpass, source, quality, **kwargs):
    """Fetch NASA's intercalibrated SMAP/SMOS daily 9 km soil moisture (NSIDC-0800) as a weather-skills Zarr."""
    import earthaccess
    import h5py
    import numpy as np
    import xarray as xr

    if kwargs.get("probe_latest") is not None:
        _login()
        today = date.today()
        results = earthaccess.search_data(
            short_name=_SHORT_NAME,
            version=_VERSION,
            temporal=((today - timedelta(days=180)).isoformat(), today.isoformat()),
        )
        days = [_granule_date(g) for g in results]
        print(max(days).isoformat() if days else "none")
        return None

    if start_time is None or end_time is None:
        raise UsageError("pass --start-time and --end-time (YYYY-MM-DD).")
    if end_time < start_time:
        raise UsageError("--end-time is before --start-time.")
    if start_time < _FIRST_DAY:
        raise UsageError(f"NSIDC-0800 starts {_FIRST_DAY.isoformat()}.")

    lat, lon = _grid()
    if bbox is None:
        rows, cols = slice(None), slice(None)
    else:
        north, west, south, east = bbox
        row_idx = np.flatnonzero((lat <= north) & (lat >= south))
        in_lon = (lon >= west) & (lon <= east) if west <= east else (lon >= west) | (lon <= east)
        col_idx = np.flatnonzero(in_lon)
        if not row_idx.size or not col_idx.size:
            raise UsageError(f"--bbox {bbox} holds no 9 km EASE-2 cell centre.")
        rows = slice(row_idx[0], row_idx[-1] + 1)
        # A box across the antimeridian reads the columns it needs, not a contiguous run.
        cols = slice(col_idx[0], col_idx[-1] + 1) if west <= east else col_idx
    lat, lon = lat[rows], lon[cols]

    _login()
    results = earthaccess.search_data(
        short_name=_SHORT_NAME,
        version=_VERSION,
        temporal=(start_time.isoformat(), end_time.isoformat()),
    )
    granules = {}
    for g in results:
        try:
            d = _granule_date(g)
        except ValueError:
            continue
        if start_time <= d <= end_time:
            granules.setdefault(d, g)
    if not granules:
        raise DataError(
            f"no {_SHORT_NAME} granule in {start_time}..{end_time}; the product runs about "
            "5 weeks behind (`--probe-latest`)."
        )
    days = [start_time + timedelta(days=i) for i in range((end_time - start_time).days + 1)]
    missing = [d for d in days if d not in granules]
    if missing:
        print(
            f"smap-smos-fetch: no granule for {len(missing)} day(s), left empty: "
            + ", ".join(d.isoformat() for d in missing[:10])
            + (" ..." if len(missing) > 10 else ""),
            file=sys.stderr,
        )
    print(
        f"smap-smos-fetch: {_SHORT_NAME} {overpass} {source} {start_time}..{end_time}: "
        f"reading {len(granules)} day(s)",
        file=sys.stderr,
    )

    layers = [
        (s, p)
        for s, p in _LAYERS
        if (source == "merged" or s == source) and (overpass == "both" or p == overpass)
    ]
    recommended_only = quality == "recommended"

    def one(day):
        try:
            (handle,) = earthaccess.open([granules[day]])
            with h5py.File(handle, "r") as h:
                fields = [_read(h, s, p, rows, cols, recommended_only) for s, p in layers]
            return day, fields
        except SkillError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"reading the {_SHORT_NAME} granule for {day} failed ({exc}).") from None

    with ThreadPoolExecutor(_WORKERS) as pool:
        read = dict(pool.map(one, sorted(granules)))
    empty = [d for d, fields in read.items() if all(f is None for f in fields)]
    if empty:
        print(
            f"smap-smos-fetch: {len(empty)} granule(s) hold no data, left empty: "
            + ", ".join(d.isoformat() for d in sorted(empty)),
            file=sys.stderr,
        )

    shape = (len(days), lat.size, lon.size)
    sm = np.full(shape, np.nan, dtype="float32")
    flag = np.zeros(shape, dtype="int8")
    for i, day in enumerate(days):
        for layer, field in zip(layers, read.get(day, [None] * len(layers)), strict=True):
            if field is None:
                continue
            fill = np.isnan(sm[i]) & ~np.isnan(field)
            sm[i][fill] = field[fill]
            flag[i][fill] = _LAYERS.index(layer) + 1

    passes = "AM and PM overpasses" if overpass == "both" else f"{overpass} overpass"
    coords = {
        "time": np.array([np.datetime64(d.isoformat(), "ns") for d in days]),
        "latitude": lat,
        "longitude": lon,
    }
    dims = ("time", "latitude", "longitude")
    ds = xr.Dataset(
        {
            "soil_moisture": (
                dims,
                sm,
                {
                    "units": "m3 m-3",
                    "standard_name": "volume_fraction_of_condensed_water_in_soil",
                    "long_name": f"Surface soil moisture ({source}, {passes})",
                    "comment": "Top ~5 cm. SMOS values are retrieved with SMAP's algorithm from "
                    "SMOS brightness temperatures calibrated to SMAP. Native 9 km EASE-Grid 2.0: "
                    "latitude spacing varies.",
                },
            )
        },
        coords=coords,
    )
    if len(layers) > 1:
        ds["soil_moisture_source"] = xr.DataArray(
            flag,
            dims=dims,
            attrs={
                "long_name": "Retrieval and overpass each soil_moisture value came from",
                "flag_values": np.arange(len(_LAYERS) + 1, dtype="int8"),
                "flag_meanings": " ".join(["none"] + [f"{s}_{p.lower()}" for s, p in _LAYERS]),
            },
        )
    ds = ensure_normalized_longitude(ds.sortby("latitude"))
    ds.attrs = {
        "Conventions": "CF-1.13",
        "title": f"SMAP/SMOS L3 radiometer 9 km soil moisture ({_SHORT_NAME} v{_VERSION}), "
        f"{passes}, {source}",
        "weather_skills_source": f"nsidc:{_SHORT_NAME}:{source}",
        "institution": "NASA National Snow and Ice Data Center DAAC",
        "source": f"{_SHORT_NAME} v{_VERSION} via NASA Earthdata",
        "references": "https://nsidc.org/data/nsidc-0800/versions/2",
    }
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

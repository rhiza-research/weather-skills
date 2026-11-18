# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "earthaccess",
#   "h5py",
#   "pyproj",
#   "requests",
#   "netcdf4",
# ]
# ///
"""Fetch daily SMAP and/or SMOS surface soil moisture (native or NASA-intercalibrated) as a weather-skills Zarr."""

# Three backends, one output:
# - native SMAP: SPL3SMP_E, NASA's enhanced L3 on the EASE-Grid 2.0 9 km grid.
# - native SMOS: CATDS (CNES) L3, the 25 km EASE-2 grid, one ~7 MB global .tgz
#   per day and overpass, open HTTPS.
# - intercalibrated: NSIDC-0800, which holds SMAP's retrieval and a SMOS one
#   made with SMAP's algorithm from SMOS brightness temperatures calibrated to
#   SMAP, both on the 9 km grid.
# The NASA files are global but stored row-chunked, so only the box's rows and
# columns are read over HTTPS; the 9 km grid's lat/lon are computed, not read.

import re
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import cf_xarray  # noqa: F401  (fail-fast probe)
import cf_xarray.units  # noqa: F401  (fail-fast probe; configures pint CF registry)
from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_coords
from weather_skills_core.standard_utils import (
    apply_write_encoding,
    bbox_subset,
    ensure_normalized_longitude,
    verify_cf_decode,
)
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_SPL3 = "SPL3SMP_E"
_NSIDC0800 = "NSIDC-0800"
_NSIDC0800_VERSION = "2"
_NASA_FIRST_DAY = date(2015, 3, 31)
_CATDS_FIRST_DAY = date(2010, 1, 17)
_FILL = -9999.0
_GRANULE_DATE_RE = re.compile(r"_(\d{8})_")
_GROUPS_0800 = {"smap": "SMAP_Based", "smos": "SMOS_Based"}
# Fill order; a value's soil_moisture_source is its 1-based place here.
_LAYERS = (("smap", "AM"), ("smos", "AM"), ("smap", "PM"), ("smos", "PM"))
_WORKERS = 4

# EASE-Grid 2.0 global 9 km (EPSG:6933): cell size and outer corner, in metres.
_ROWS, _COLS = 1624, 3856
_CELL = 9008.055210146
_X0, _Y0 = -17367530.445161499, 7314540.830638

_CATDS_BASE = "https://data.catds.fr/cpdc/Land_products/GRIDDED/L3SM"
_LAST_RE07 = date(2021, 5, 24)
# SMOS crosses the equator ascending at 06:00 and descending at 18:00 local time.
_CATDS_PRODUCT = {"AM": "MIR_CLF31A", "PM": "MIR_CLF31D"}
# Widest CATDS cell (degrees) a 9 km cell centre may be from the 25 km cell it takes.
_CATDS_NEAREST_TOL = 0.3
_CATDS_WARN_DAYS = 366
_CATDS_ACK = (
    'These data were obtained from the "Centre Aval de Traitement des Données SMOS" (CATDS), '
    'operated for the "Centre National d\'Etudes Spatiales" (CNES, France) by IFREMER '
    "(Brest, France)"
)

_SM_STANDARD_NAME = "volume_fraction_of_condensed_water_in_soil"
_SM_UNITS = "m3 m-3"
_CF_CONVENTIONS = "CF-1.13"
_TIME_UNITS = "days since 1970-01-01 00:00:00"
_TIME_CALENDAR = "proleptic_gregorian"
_GRID_MAPPING_NAME = "latitude_longitude"

_AUTH_FAIL_MSG = (
    "Earthdata authentication failed; configure EARTHDATA_USERNAME/"
    "EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) or a urs.earthdata.nasa.gov entry in "
    "~/.netrc, then retry."
)


def _ease9_grid():
    """1-D latitude (north to south) and longitude of the EASE-2 9 km cell centres."""
    import numpy as np
    from pyproj import Transformer

    to_ll = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True)
    x = _X0 + (np.arange(_COLS) + 0.5) * _CELL
    y = _Y0 - (np.arange(_ROWS) + 0.5) * _CELL
    lon, _ = to_ll.transform(x, np.zeros_like(x))
    _, lat = to_ll.transform(np.zeros_like(y), y)
    return lat, lon


def _box_index(lat, lon, bbox):
    """Row and column selectors of the grid cells whose centres fall in ``bbox``."""
    import numpy as np

    if bbox is None:
        return slice(None), slice(None)
    north, west, south, east = bbox
    row_idx = np.flatnonzero((lat <= north) & (lat >= south))
    in_lon = (lon >= west) & (lon <= east) if west <= east else (lon >= west) | (lon <= east)
    col_idx = np.flatnonzero(in_lon)
    if not row_idx.size or not col_idx.size:
        raise UsageError(f"--bbox {bbox} holds no 9 km EASE-2 cell centre.")
    rows = slice(row_idx[0], row_idx[-1] + 1)
    # A box across the antimeridian reads the columns it needs, not a contiguous run.
    cols = slice(col_idx[0], col_idx[-1] + 1) if west <= east else col_idx
    return rows, cols


def _granule_date(granule) -> date:
    for url in granule.data_links():
        m = _GRANULE_DATE_RE.search(url.rsplit("/", 1)[-1])
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007
    raise ValueError("could not parse a YYYYMMDD date from the granule file name")


def _is_auth_error(exc: Exception) -> bool:
    for status in (
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
    ):
        if status in (401, 403):
            return True
    msg = str(exc).lower()
    return "401 unauthorized" in msg or "403 forbidden" in msg


def _login() -> None:
    """Authenticate via environment then netrc; never falls through to interactive."""
    import earthaccess
    from earthaccess.exceptions import LoginAttemptFailure, LoginStrategyUnavailable

    for strategy in ("environment", "netrc"):
        try:
            auth = earthaccess.login(strategy=strategy)
        except LoginStrategyUnavailable:
            continue
        except LoginAttemptFailure:
            raise DataError(_AUTH_FAIL_MSG) from None
        except Exception as exc:
            if _is_auth_error(exc):
                raise DataError(_AUTH_FAIL_MSG) from None
            raise
        if getattr(auth, "authenticated", False):
            return
    raise DataError(_AUTH_FAIL_MSG)


def _earthaccess_call(fn, *args, **kwargs):
    """Call an earthaccess function; map 401/403 to the auth message."""
    try:
        return fn(*args, **kwargs)
    except SkillError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise DataError(_AUTH_FAIL_MSG) from None
        raise


def _nasa_granules(short_name, version, start, end):
    """{day: granule} for a NASA collection over ``start..end``."""
    import earthaccess

    kwargs = {"short_name": short_name, "temporal": (start.isoformat(), end.isoformat())}
    if version:
        kwargs["version"] = version
    granules = {}
    for g in _earthaccess_call(earthaccess.search_data, **kwargs):
        try:
            d = _granule_date(g)
        except ValueError:
            continue
        if start <= d <= end:
            granules.setdefault(d, g)
    return granules


def _read_nasa(handle, product, sensor, overpass, rows, cols, recommended_only, day_iso):
    """One retrieval's soil moisture for the box, NaN where missing or masked; None if the field is empty.

    Fill, out-of-valid-range and (with ``recommended_only``) retrievals whose
    ``retrieval_qual_flag`` bit 0 is set (not recommended) become NaN.
    """
    import numpy as np
    from cf_xarray.units import units as ureg

    grp_name = f"Soil_Moisture_Retrieval_Data_{overpass}"
    if product == _SPL3:
        # SPL3SMP_E's PM datasets carry a "_pm" suffix; its AM ones none.
        root, suffix = handle, "_pm" if overpass == "PM" else ""
    else:
        root, suffix = handle[_GROUPS_0800[sensor]], f"_{overpass.lower()}"
    try:
        grp = root[grp_name]
        dset = grp[f"soil_moisture{suffix}"]
        qual = grp[f"retrieval_qual_flag{suffix}"]
    except KeyError as exc:
        raise RuntimeError(
            f"the {product} granule for {day_iso} has no {sensor} {overpass} soil moisture "
            f"({exc.args[0]})."
        ) from None
    # Some granules are placeholders with nothing stored, which h5py reads as zeros.
    if dset.id.get_storage_size() == 0:
        return None
    raw_units = dset.attrs.get("units")
    if raw_units is None:
        raise RuntimeError(
            f"the {product} granule for {day_iso} has no units on its soil moisture; "
            "cannot confirm it is a volumetric ratio and will not fabricate one."
        )
    if isinstance(raw_units, bytes):
        raw_units = raw_units.decode("utf-8", "replace")
    try:
        ratio = ureg.Quantity(1.0, str(raw_units).strip()).to("dimensionless").magnitude
    except Exception:
        ratio = None
    if ratio is None or not np.isclose(ratio, 1.0):
        raise RuntimeError(
            f"the {product} granule for {day_iso} has soil moisture units {raw_units!r}; "
            "expected a dimensionless volumetric ratio (cm**3/cm**3), refusing to relabel it."
        )
    sm = dset[rows, cols].astype("float32")
    lo = float(dset.attrs.get("valid_min", 0.0))
    hi = float(dset.attrs.get("valid_max", 1.0))
    bad = (sm == _FILL) | (sm < lo) | (sm > hi)
    if recommended_only:
        bad |= (qual[rows, cols] & 1) != 0
    return np.where(bad, np.nan, sm)


def _read_nasa_day(granule, product, layers, rows, cols, recommended_only, day_iso):
    """Every layer's field from one NASA granule, opened once over HTTPS."""
    import earthaccess
    import h5py

    try:
        (handle,) = _earthaccess_call(earthaccess.open, [granule])
        with h5py.File(handle, "r") as h:
            return [
                _read_nasa(h, product, s, p, rows, cols, recommended_only, day_iso)
                for s, p in layers
            ]
    except SkillError:
        raise
    except RuntimeError as exc:
        raise DataError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"reading the {product} granule for {day_iso} failed ({exc}).") from None


def _catds_day_dir(day, overpass):
    archive = "RE07" if day <= _LAST_RE07 else "OPER"
    return f"{_CATDS_BASE}/{archive}/{_CATDS_PRODUCT[overpass]}/{day.year}/{day.timetuple().tm_yday:03d}/"


def _catds_listing(session, url, pattern):
    resp = session.get(url, timeout=60)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return sorted(set(re.findall(pattern, resp.text)))


def _catds_latest(session, overpass):
    top = f"{_CATDS_BASE}/OPER/{_CATDS_PRODUCT[overpass]}/"
    year = _catds_listing(session, top, r'href="(\d{4})/?"')[-1]
    doy = _catds_listing(session, f"{top}{year}/", r'href="(\d{3})/?"')[-1]
    return date(int(year), 1, 1) + timedelta(days=int(doy) - 1)


def _catds_day(session, day, overpass, bbox, workdir):
    """The day's CATDS soil moisture over ``bbox`` on its 25 km grid, or None when CATDS has no file."""
    import requests
    import xarray as xr

    url = _catds_day_dir(day, overpass)
    try:
        names = _catds_listing(session, url, r'href="(SM_[^"/]+\.tgz)"')
        if not names:
            return None
        resp = session.get(url + names[-1], timeout=300)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DataError(f"CATDS download failed for {day} ({exc}).") from None
    tgz = Path(workdir) / names[-1]
    tgz.write_bytes(resp.content)
    with tarfile.open(tgz) as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith(".nc")), None)
        if member is None:
            raise DataError(f"{names[-1]} holds no NetCDF.")
        tar.extract(member, workdir, filter="data")
    tgz.unlink()
    path = Path(workdir) / member.name
    with xr.open_dataset(path) as raw:
        ds = raw[["Soil_Moisture"]].rename(lat="latitude", lon="longitude").sortby("latitude")
        ds = ensure_normalized_longitude(ds)
        if bbox is not None:
            ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
        da = ds["Soil_Moisture"].astype("float32").load()
    path.unlink()
    return da


def _padded_bbox(bbox):
    """``bbox`` widened by one CATDS cell, so every 9 km cell near its edge has a 25 km neighbour."""
    if bbox is None:
        return None
    north, west, south, east = bbox
    if west <= east and east - west >= 360 - 2 * _CATDS_NEAREST_TOL:
        return (
            min(north + _CATDS_NEAREST_TOL, 90.0),
            -180.0,
            max(south - _CATDS_NEAREST_TOL, -90.0),
            180.0,
        )

    def wrap(x):
        return ((x + 180.0) % 360.0) - 180.0

    return (
        min(north + _CATDS_NEAREST_TOL, 90.0),
        wrap(west - _CATDS_NEAREST_TOL),
        max(south - _CATDS_NEAREST_TOL, -90.0),
        wrap(east + _CATDS_NEAREST_TOL),
    )


def _note_missing(label, days):
    if days:
        print(
            f"smap-smos-fetch: no {label} file for {len(days)} day(s), left empty: "
            + ", ".join(d.isoformat() for d in days[:10])
            + (" ..." if len(days) > 10 else ""),
            file=sys.stderr,
        )


def _probe_latest(sensor, product, overpasses):
    import requests

    today = datetime.now(UTC).date()
    latest = []
    if product == "intercalibrated":
        _login()
        days = _nasa_granules(_NSIDC0800, _NSIDC0800_VERSION, today - timedelta(days=180), today)
        latest.append(max(days) if days else None)
    else:
        if sensor in ("smap", "both"):
            _login()
            days = _nasa_granules(_SPL3, None, today - timedelta(days=21), today)
            latest.append(max(days) if days else None)
        if sensor in ("smos", "both"):
            session = requests.Session()
            for p in overpasses:
                try:
                    latest.append(_catds_latest(session, p))
                except Exception as exc:  # noqa: BLE001
                    raise DataError(f"could not list CATDS ({exc}).") from None
    print("none" if None in latest else min(latest).isoformat())


def _stamp_cf(ds, title, source, institution, references) -> None:
    import xarray as xr

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    ds.attrs.clear()
    ds.attrs.update(
        Conventions=_CF_CONVENTIONS,
        title=title,
        source=source,
        institution=institution,
        references=references,
        history=f"{now_iso}: fetched via smap-smos-fetch v{_SKILL_VERSION}",
    )
    stamp_cf_coords(ds)
    ds["soil_moisture"].attrs["grid_mapping"] = _GRID_MAPPING_NAME
    ds[_GRID_MAPPING_NAME] = xr.DataArray(0, attrs={})
    ds[_GRID_MAPPING_NAME].attrs.update(
        grid_mapping_name="latitude_longitude",
        longitude_of_prime_meridian=0.0,
        semi_major_axis=6378137.0,
        inverse_flattening=298.257223563,
    )


@weather_skill(name="smap-smos-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--sensor",
    choices=("smap", "smos", "both"),
    default="smap",
    help="smap (default), smos, or both: SMAP, gaps filled from SMOS.",
)
@weather_skill.argument(
    "--product",
    choices=("native", "intercalibrated"),
    default="native",
    help=(
        "native (default): SMAP SPL3SMP_E and CATDS SMOS, each as its own team retrieves it; "
        "intercalibrated: NASA's NSIDC-0800, SMOS retrieved the SMAP way."
    ),
)
@weather_skill.argument(
    "--overpass",
    choices=("AM", "PM", "both"),
    default="AM",
    help="AM = 6am, PM = 6pm local overpass; both = AM, gaps filled from PM. Default AM.",
)
@weather_skill.argument(
    "--quality",
    choices=("recommended", "all"),
    default="recommended",
    help=(
        "recommended (default) keeps only NASA retrievals whose retrieval_qual_flag marks "
        "them recommended quality; all also keeps not-recommended ones. CATDS SMOS is kept as served."
    ),
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest YYYY-MM-DD that every source --sensor/--product/--overpass needs "
        "has (or none) on stdout and exit. Does not download fields."
    ),
)
def fetch(start_time, end_time, bbox, sensor, product, overpass, quality, probe_latest, **kwargs):
    """Fetch daily SMAP and/or SMOS surface soil moisture (native or NASA-intercalibrated) as a weather-skills Zarr."""
    import numpy as np
    import requests
    import xarray as xr

    overpasses = ("AM", "PM") if overpass == "both" else (overpass,)
    if probe_latest is not None:
        _probe_latest(sensor, product, overpasses)
        return None

    if end_time < start_time:
        raise UsageError("--end-time is before --start-time.")
    first = (
        _NASA_FIRST_DAY if product == "intercalibrated" or sensor != "smos" else _CATDS_FIRST_DAY
    )
    if start_time < first:
        raise UsageError(f"--sensor {sensor} --product {product} starts {first.isoformat()}.")

    layers = [(s, p) for s, p in _LAYERS if (sensor == "both" or s == sensor) and p in overpasses]
    days = [start_time + timedelta(days=i) for i in range((end_time - start_time).days + 1)]
    recommended_only = quality == "recommended"
    # Per day, one field (or None) per layer, all on the output grid.
    fields = {d: [None] * len(layers) for d in days}

    # The output grid: the EASE-2 9 km grid, except native SMOS alone, which stays on its 25 km grid.
    on_9km = product == "intercalibrated" or sensor != "smos"
    if on_9km:
        lat, lon = _ease9_grid()
        rows, cols = _box_index(lat, lon, bbox)
        lat, lon = lat[rows], lon[cols]
    else:
        lat = lon = None

    print(
        f"smap-smos-fetch: {sensor} {product} {overpass} {start_time}..{end_time}: "
        f"{len(days)} day(s)",
        file=sys.stderr,
    )

    nasa = (_NSIDC0800, _NSIDC0800_VERSION) if product == "intercalibrated" else (_SPL3, None)
    nasa_layers = [
        (i, layer)
        for i, layer in enumerate(layers)
        if product == "intercalibrated" or layer[0] == "smap"
    ]
    if nasa_layers:
        _login()
        granules = _nasa_granules(nasa[0], nasa[1], start_time, end_time)
        _note_missing(nasa[0], [d for d in days if d not in granules])

        def one_nasa(day):
            return day, _read_nasa_day(
                granules[day],
                nasa[0],
                [layer for _, layer in nasa_layers],
                rows,
                cols,
                recommended_only,
                day.isoformat(),
            )

        with ThreadPoolExecutor(_WORKERS) as pool:
            for day, read in pool.map(one_nasa, sorted(granules)):
                for (i, _), field in zip(nasa_layers, read, strict=True):
                    fields[day][i] = field

    catds_layers = [
        (i, layer) for i, layer in enumerate(layers) if product == "native" and layer[0] == "smos"
    ]
    if catds_layers:
        if len(days) * len(catds_layers) > _CATDS_WARN_DAYS:
            print(
                f"smap-smos-fetch: warning: {len(days) * len(catds_layers)} CATDS files is about "
                f"{7 * len(days) * len(catds_layers) // 1000} GB of global files; expect a long download.",
                file=sys.stderr,
            )
        session = requests.Session()
        catds_bbox = _padded_bbox(bbox) if on_9km else bbox
        jobs = [(day, i, p) for day in days for i, (_, p) in catds_layers]
        with tempfile.TemporaryDirectory(prefix="smap-smos-fetch-") as tmp:

            def one_catds(job):
                day, i, p = job
                return job, _catds_day(session, day, p, catds_bbox, tmp)

            with ThreadPoolExecutor(_WORKERS) as pool:
                results = list(pool.map(one_catds, jobs))
        for p in overpasses:
            _note_missing(
                f"CATDS {_CATDS_PRODUCT[p]}",
                [day for (day, _, q), da in results if q == p and da is None],
            )
        catds = [((day, i), da) for (day, i, _), da in results if da is not None]
        if catds and not on_9km:
            # Native SMOS alone: the output grid is CATDS's own (north to south, like the 9 km grid).
            ref = catds[0][1].sortby("latitude", ascending=False)
            lat, lon = ref["latitude"].values, ref["longitude"].values
        for (day, i), da in catds:
            if on_9km:
                # Each 9 km cell takes the 25 km SMOS cell its centre is nearest to.
                da = da.reindex(
                    latitude=lat, longitude=lon, method="nearest", tolerance=_CATDS_NEAREST_TOL
                )
            else:
                da = da.reindex(latitude=lat, longitude=lon)
            fields[day][i] = da.values

    if lat is None:
        raise DataError(f"CATDS has no {overpass} SMOS files for {start_time}..{end_time}.")

    shape = (len(days), lat.size, lon.size)
    sm = np.full(shape, np.nan, dtype="float32")
    flag = np.zeros(shape, dtype="int8")
    for t, day in enumerate(days):
        for layer, field in zip(layers, fields[day], strict=True):
            if field is None:
                continue
            fill = np.isnan(sm[t]) & ~np.isnan(field)
            sm[t][fill] = field[fill]
            flag[t][fill] = _LAYERS.index(layer) + 1

    empty = [d for t, d in enumerate(days) if np.isnan(sm[t]).all()]
    if len(empty) == len(days):
        raise DataError(
            f"no usable {sensor} {overpass} retrievals in the requested area within "
            f"{start_time}..{end_time}; try --overpass both, another --sensor, or a wider window."
        )
    if empty:
        print(
            f"smap-smos-fetch: {len(empty)} day(s) have no usable retrievals in the requested "
            "area (no file, an empty file, the overpass missed it, or every retrieval there "
            "was masked), left empty (NaN): "
            + ", ".join(d.isoformat() for d in empty[:10])
            + (" ..." if len(empty) > 10 else ""),
            file=sys.stderr,
        )

    passes = "AM and PM overpasses" if overpass == "both" else f"{overpass} overpass"
    what = {"smap": "SMAP", "smos": "SMOS", "both": "SMAP, gaps filled from SMOS"}[sensor]
    dims = ("time", "latitude", "longitude")
    ds = xr.Dataset(
        {
            "soil_moisture": (
                dims,
                sm,
                {
                    "standard_name": _SM_STANDARD_NAME,
                    "units": _SM_UNITS,
                    "long_name": f"surface soil moisture ({what}, {passes})",
                    "comment": "Top ~5 cm. Native EASE-Grid 2.0 cell centres: latitude spacing varies."
                    + (
                        " SMOS values are retrieved with SMAP's algorithm from SMOS brightness "
                        "temperatures calibrated to SMAP."
                        if product == "intercalibrated" and sensor != "smap"
                        else ""
                    ),
                },
            )
        },
        coords={
            "time": np.array([np.datetime64(d.isoformat(), "ns") for d in days]),
            "latitude": lat,
            "longitude": lon,
        },
    )
    if len(layers) > 1:
        ds["soil_moisture_source"] = xr.DataArray(
            flag,
            dims=dims,
            attrs={
                "long_name": "retrieval and overpass each soil_moisture value came from",
                "flag_values": np.arange(len(_LAYERS) + 1, dtype="int8"),
                "flag_meanings": " ".join(["none"] + [f"{s}_{p.lower()}" for s, p in _LAYERS]),
            },
        )
    ds = ds.sortby("latitude")

    if product == "intercalibrated":
        _stamp_cf(
            ds,
            title=f"SMAP/SMOS L3 radiometer 9 km soil moisture ({_NSIDC0800} v{_NSIDC0800_VERSION}), {sensor}, {passes}",
            source=f"{_NSIDC0800} v{_NSIDC0800_VERSION} via NASA Earthdata",
            institution="NASA National Snow and Ice Data Center DAAC",
            references="https://nsidc.org/data/nsidc-0800/versions/2",
        )
        source_tag = f"nsidc:{_NSIDC0800}:{sensor}"
    else:
        parts = []
        if sensor != "smos":
            parts.append(
                (
                    f"SMAP {_SPL3} (Enhanced L3 radiometer 9 km)",
                    "NASA National Snow and Ice Data Center DAAC",
                    "https://nsidc.org/data/spl3smp_e",
                    f"nsidc:{_SPL3}",
                )
            )
        if sensor != "smap":
            parts.append(
                (
                    f"SMOS L3 (CATDS {'/'.join(_CATDS_PRODUCT[p] for p in overpasses)})",
                    "CATDS, operated for CNES by IFREMER",
                    "https://doi.org/10.5194/essd-9-293-2017",
                    "catds:smos-l3sm",
                )
            )
        _stamp_cf(
            ds,
            title=f"{what} daily surface soil moisture, {passes}",
            source="; ".join(p[0] for p in parts),
            institution="; ".join(p[1] for p in parts),
            references=" ".join(p[2] for p in parts),
        )
        source_tag = "+".join(p[3] for p in parts)
        if sensor != "smap":
            ds.attrs["acknowledgement"] = _CATDS_ACK
    ds = ensure_normalized_longitude(ds, lon_dim="longitude")
    ds.attrs["weather_skills_source"] = source_tag
    verify_cf_decode(ds)
    apply_write_encoding(
        ds,
        time_units=_TIME_UNITS,
        time_calendar=_TIME_CALENDAR,
        fills={"soil_moisture": np.float32(np.nan)},
    )
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

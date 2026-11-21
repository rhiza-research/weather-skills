# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.4.0",
#   "earthaccess",
#   "ecmwf-datastores-client==0.4.2",
#   "h5py",
#   "pyproj",
#   "requests",
#   "netcdf4",
# ]
# ///
"""Fetch daily satellite surface soil moisture (ASCAT, C3S COMBINED, SMAP, SMOS, or SMAP+SMOS) as a weather-skills Zarr."""

# Three backends, one daily output:
# - CDS (ascat, combined): C3S satellite-soil-moisture, 0.25 deg daily, one
#   ~1 MB global file per day, requested a month at a time; the box is cut
#   after download.
# - NASA Earthdata (smap, smap-smos): SPL3SMP_E and NSIDC-0800 on the
#   EASE-Grid 2.0 9 km grid. Files are row-chunked, so only the box is read
#   over HTTPS; the grid's lat/lon are computed, not read.
# - CATDS (smos): ESA's SMOS L3 on the 25 km EASE-2 grid, one ~7 MB global
#   .tgz per day and overpass, open HTTPS.

import datetime as dt
import os
import re
import sys
import tarfile
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path

from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_NAME = "soil-moisture-fetch"
_WORKERS = 4
_WARN_DAYS = 366
_VOLUMETRIC = {"units": "m3 m-3", "standard_name": "volume_fraction_of_condensed_water_in_soil"}

# --dataset -> what it is. "first" is the first day served.
_DATASETS = {
    "ascat": {
        "first": dt.date(1991, 8, 5),
        "title": "C3S satellite surface soil moisture, ACTIVE (Metop ASCAT, ERS AMI)",
        "institution": "TU Wien / EODC for the Copernicus Climate Change Service",
        "references": "https://doi.org/10.24381/cds.d7782f18",
        "source_tag": "cds:satellite-soil-moisture:active",
    },
    "combined": {
        "first": dt.date(1978, 11, 1),
        "title": "C3S satellite surface soil moisture, COMBINED (active and passive sensors merged)",
        "institution": "TU Wien / EODC for the Copernicus Climate Change Service",
        "references": "https://doi.org/10.24381/cds.d7782f18",
        "source_tag": "cds:satellite-soil-moisture:combined",
    },
    "smap": {
        "first": dt.date(2015, 3, 31),
        "title": "SMAP SPL3SMP_E daily surface soil moisture (Enhanced L3 radiometer 9 km)",
        "institution": "NASA National Snow and Ice Data Center DAAC",
        "references": "https://nsidc.org/data/spl3smp_e",
        "source_tag": "nsidc:SPL3SMP_E",
    },
    "smos": {
        "first": dt.date(2010, 1, 17),
        "title": "SMOS L3 daily surface soil moisture (CATDS)",
        "institution": "CATDS, operated for CNES by IFREMER",
        "references": "https://doi.org/10.5194/essd-9-293-2017",
        "source_tag": "catds:smos-l3sm",
    },
    "smap-smos": {
        "first": dt.date(2015, 3, 31),
        "title": "SMAP/SMOS L3 radiometer 9 km soil moisture (NSIDC-0800 v2), SMAP gaps filled from SMOS",
        "institution": "NASA National Snow and Ice Data Center DAAC",
        "references": "https://nsidc.org/data/nsidc-0800/versions/2",
        "source_tag": "nsidc:NSIDC-0800",
    },
}


def _note(msg, days=None):
    """A stderr note; with ``days``, lists the first ten, and an empty list prints nothing."""
    if days is not None:
        if not days:
            return
        msg = f"{msg} for {len(days)} day(s): " + ", ".join(d.isoformat() for d in days[:10])
        msg += " ..." if len(days) > 10 else ""
    print(f"{_NAME}: {msg}", file=sys.stderr)


# --- CDS ---------------------------------------------------------------------------

# The CDS API base URL is a fixed, public endpoint, not a secret.
_CDS_URL = "https://cds.climate.copernicus.eu/api"
_CDS_HOST = "cds.climate.copernicus.eu"
_CDS_DATASET = "satellite-soil-moisture"
# --dataset -> (type_of_sensor, CDS variable, output name, units attrs).
_CDS_VARS = {
    "ascat": (
        "active",
        "surface_soil_moisture_saturation",
        "soil_moisture_saturation",
        {
            "units": "percent",
            "comment": "Relative to the wettest the soil gets at that cell (100 = saturated), "
            "top ~2 cm. Not volumetric (m3 m-3).",
        },
    ),
    "combined": ("combined", "surface_soil_moisture_volumetric", "soil_moisture", _VOLUMETRIC),
}
_POLL_SECONDS = 10
_POLL_MAX_SECONDS = 3600


def _cds_days(dataset):
    """{date: (version, type_of_record)} for every daily file the CDS lists for ``dataset``, newest version."""
    import requests

    sensor, variable, *_ = _CDS_VARS[dataset]
    base = f"{_CDS_URL}/catalogue/v1/collections/{_CDS_DATASET}"
    try:
        links = requests.get(base, timeout=30).json()["links"]
        url = next(link["href"] for link in links if link["rel"] == "constraints")
        constraints = requests.get(url, timeout=60).json()
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"could not read the CDS catalogue for {_CDS_DATASET} ({exc}).") from None

    # Each constraints row lists the values that combine into valid requests.
    want = {"type_of_sensor": sensor, "time_aggregation": "daily", "variable": variable}
    days = {}
    for row in constraints:
        if any(v not in row.get(k, []) for k, v in want.items()):
            continue
        versions = [v for v in row["version"] if not v.startswith("deprecated")]
        for version, record, y, m, d in product(
            versions, row["type_of_record"], row["year"], row["month"], row["day"]
        ):
            try:
                day = dt.date(int(y), int(m), int(d))
            except ValueError:  # 31 February and the like
                continue
            # v202505_2 > v202505 > v202312: the newest version wins.
            if version > days.get(day, ("",))[0]:
                days[day] = (version, record)
    if not days:
        raise DataError(f"the CDS catalogue lists no daily {sensor} files for {_CDS_DATASET}.")
    return days


def _fetch_cds(dataset, days, bbox, uncertainty):
    """The CDS's daily files for ``days`` over ``bbox``, as one Dataset (days the CDS lacks are absent)."""
    import requests
    import xarray as xr
    from ecmwf.datastores import Client
    from ecmwf.datastores.processing import ProcessingFailedError

    sensor, variable, out_name, attrs = _CDS_VARS[dataset]
    available = _cds_days(dataset)
    _note("not on the CDS (left empty)", [d for d in days if d not in available])
    # One request per (version, record, month).
    groups = {}
    for day in days:
        if day in available:
            groups.setdefault((*available[day], day.year, day.month), []).append(day)
    if not groups:
        raise DataError(
            f"the CDS lists no {dataset} soil moisture for {days[0]}..{days[-1]}; the last day "
            f"is {max(available).isoformat()} (`--probe-latest`)."
        )

    # Credentials: CDS_API_KEY, else a ~/.cdsapirc whose url is the CDS.
    key = os.environ.get("CDS_API_KEY")
    rc = Path("~/.cdsapirc").expanduser()
    if not key and rc.is_file():
        fields = dict(line.partition(":")[::2] for line in rc.read_text().splitlines())
        fields = {k.strip(): v.strip() for k, v in fields.items()}
        if _CDS_HOST in fields.get("url", ""):
            key = fields.get("key")
    if not key:
        raise UsageError(
            "no CDS credentials: set CDS_API_KEY (your personal token from "
            f"https://{_CDS_HOST}/profile), or write a ~/.cdsapirc whose url is {_CDS_URL}."
        )
    client = Client(url=_CDS_URL, key=key)
    _note(f"submitting {len(groups)} CDS request(s)")
    remotes = []
    for (version, record, year, month), group in groups.items():
        request = {
            "variable": [variable],
            "type_of_sensor": [sensor],
            "time_aggregation": ["daily"],
            "type_of_record": [record],
            "version": [version],
            "year": [str(year)],
            "month": [f"{month:02d}"],
            "day": [f"{d.day:02d}" for d in group],
        }
        try:
            remotes.append(client.submit(_CDS_DATASET, request))
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 403 and "licence" in str(exc).lower():
                raise DataError(
                    f"ERROR: CDS retrieval blocked: licence not accepted on {_CDS_DATASET}.\n"
                    f"Action: open https://{_CDS_HOST}/datasets/{_CDS_DATASET}?tab=download"
                    "#manage-licences in a browser, log in, accept the licence, then "
                    "re-run this skill.",
                    prefix=False,
                ) from None
            if status in (401, 403):
                raise DataError(
                    f"CDS rejected the key (HTTP {status}); check CDS_API_KEY / ~/.cdsapirc."
                ) from None
            raise DataError(f"CDS submit failed for {_CDS_DATASET} ({exc}).") from None

    waited = 0
    while True:
        try:
            if all(r.results_ready for r in remotes):
                break
        except ProcessingFailedError as exc:
            raise DataError(f"CDS reported no data for {_CDS_DATASET} ({exc}).") from None
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"polling the CDS for {_CDS_DATASET} failed ({exc}).") from None
        if waited >= _POLL_MAX_SECONDS:
            raise DataError(
                f"CDS job was still queued after {_POLL_MAX_SECONDS}s; re-run later or "
                "request fewer days."
            )
        time.sleep(_POLL_SECONDS)
        waited += _POLL_SECONDS

    names = {"sm": out_name} | (
        {"sm_uncertainty": f"{out_name}_uncertainty"} if uncertainty else {}
    )
    pieces = []
    with tempfile.TemporaryDirectory(prefix=f"{_NAME}-") as tmp:
        for i, remote in enumerate(remotes):
            path = Path(tmp) / f"r{i}.zip"
            remote.download(str(path))
            # A request returns a zip of daily netCDFs, or one netCDF for a single day.
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(Path(tmp) / f"r{i}")
                files = sorted((Path(tmp) / f"r{i}").glob("*.nc"))
            else:
                files = [path]
            for f in files:
                with xr.open_dataset(f) as day:
                    day = day[list(names)].rename(lat="latitude", lon="longitude")
                    day = ensure_normalized_longitude(day.sortby("latitude"))
                    if bbox is not None:
                        day = bbox_subset(day, bbox, lat_dim="latitude", lon_dim="longitude")
                    pieces.append(day.load())
    if not pieces:
        raise DataError("the CDS returned no files.")

    ds = xr.concat(pieces, dim="time").sortby("time").rename(names)
    for k, name in names.items():
        long_name = "surface soil moisture" + (" uncertainty" if k == "sm_uncertainty" else "")
        ds[name] = ds[name].astype("float32")
        ds[name].attrs = {"long_name": f"{long_name} ({dataset})", **attrs}
    ds.attrs["source"] = (
        f"{_CDS_URL} ({_CDS_DATASET}, {sensor}, versions "
        + ", ".join(sorted({v for v, *_ in groups}))
        + ")"
    )
    ds.attrs["license"] = "Copernicus Data License (CC-BY-4.0)"
    return ds


# --- NASA --------------------------------------------------------------------------

_NASA = {"smap": ("SPL3SMP_E", None), "smap-smos": ("NSIDC-0800", "2")}
_FILL = -9999.0
_READ_TRIES = 3
_GRANULE_DATE_RE = re.compile(r"_(\d{8})_")
# EASE-Grid 2.0 global 9 km (EPSG:6933): cell size and outer corner, in metres.
_ROWS, _COLS = 1624, 3856
_CELL = 9008.055210146
_X0, _Y0 = -17367530.445161499, 7314540.830638
_AUTH_FAIL_MSG = (
    "Earthdata authentication failed; configure EARTHDATA_USERNAME/"
    "EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) or a urs.earthdata.nasa.gov entry in "
    "~/.netrc, then retry."
)


def _ease9_box(bbox):
    """Latitudes, longitudes, and row/column selectors of the EASE-2 9 km cell centres in ``bbox``."""
    import numpy as np
    from pyproj import Transformer

    to_ll = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True)
    x = _X0 + (np.arange(_COLS) + 0.5) * _CELL
    y = _Y0 - (np.arange(_ROWS) + 0.5) * _CELL
    lon, _ = to_ll.transform(x, np.zeros_like(x))
    _, lat = to_ll.transform(np.zeros_like(y), y)
    if bbox is None:
        return lat, lon, slice(None), slice(None)
    north, west, south, east = bbox
    row_idx = np.flatnonzero((lat <= north) & (lat >= south))
    in_lon = (lon >= west) & (lon <= east) if west <= east else (lon >= west) | (lon <= east)
    col_idx = np.flatnonzero(in_lon)
    if not row_idx.size or not col_idx.size:
        raise UsageError(f"--bbox {bbox} holds no 9 km EASE-2 cell centre.")
    rows = slice(row_idx[0], row_idx[-1] + 1)
    # A box across the antimeridian reads the columns it needs, not a contiguous run.
    cols = slice(col_idx[0], col_idx[-1] + 1) if west <= east else col_idx
    return lat[rows], lon[cols], rows, cols


def _login():
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
        if getattr(auth, "authenticated", False):
            return
    raise DataError(_AUTH_FAIL_MSG)


def _nasa_granules(dataset, start, end):
    """{day: newest granule} for ``dataset``'s NASA collection over ``start..end``."""
    import earthaccess

    short_name, version = _NASA[dataset]
    kwargs = {"short_name": short_name, "temporal": (start.isoformat(), end.isoformat())}
    if version:
        kwargs["version"] = version
    granules, names = {}, {}
    for g in earthaccess.search_data(**kwargs):
        name = next((u.rsplit("/", 1)[-1] for u in g.data_links()), "")
        m = _GRANULE_DATE_RE.search(name)
        if m:
            day = dt.datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007
            # During a reprocessing two files can share a day; the newest name
            # (higher CRID or version, e.g. R19240_001 over R18290_001) wins.
            if start <= day <= end and name > names.get(day, ""):
                granules[day], names[day] = g, name
    return granules


def _read_nasa(granule, dataset, layers, rows, cols, recommended_only, day_iso):
    """Each (sensor, overpass) layer's soil moisture for the box from one NASA granule, opened once over HTTPS.

    A layer is NaN where fill, out of the valid range, or (with ``recommended_only``)
    flagged not recommended (``retrieval_qual_flag`` bit 0), and None when the granule
    stores nothing for it. A failed read is retried, so one dropped connection does not
    end a long fetch.
    """
    import earthaccess
    import h5py
    import numpy as np
    from cf_xarray.units import units as ureg

    def layer(h, sensor, overpass):
        if dataset == "smap":
            # SPL3SMP_E's PM datasets carry a "_pm" suffix; its AM ones none.
            root, suffix = h, "_pm" if overpass == "PM" else ""
        else:
            root, suffix = h[f"{sensor.upper()}_Based"], f"_{overpass.lower()}"
        try:
            grp = root[f"Soil_Moisture_Retrieval_Data_{overpass}"]
            dset = grp[f"soil_moisture{suffix}"]
            qual = grp[f"retrieval_qual_flag{suffix}"]
        except KeyError as exc:
            raise DataError(
                f"the {dataset} granule for {day_iso} has no {sensor} {overpass} soil moisture "
                f"({exc.args[0]})."
            ) from None
        # Some granules are placeholders with nothing stored, which h5py reads as zeros.
        if dset.id.get_storage_size() == 0:
            return None
        raw_units = dset.attrs.get("units")
        if isinstance(raw_units, bytes):
            raw_units = raw_units.decode("utf-8", "replace")
        try:
            ratio = ureg.Quantity(1.0, str(raw_units).strip()).to("dimensionless").magnitude
        except Exception:  # noqa: BLE001
            ratio = None
        if raw_units is None or ratio is None or not np.isclose(ratio, 1.0):
            raise DataError(
                f"the {dataset} granule for {day_iso} has soil moisture units {raw_units!r}; "
                "expected a dimensionless volumetric ratio (cm**3/cm**3), refusing to relabel it."
            )
        sm = dset[rows, cols].astype("float32")
        lo = float(dset.attrs.get("valid_min", 0.0))
        hi = float(dset.attrs.get("valid_max", 1.0))
        bad = (sm == _FILL) | (sm < lo) | (sm > hi)
        if recommended_only:
            bad |= (qual[rows, cols] & 1) != 0
        return np.where(bad, np.nan, sm)

    for attempt in range(1, _READ_TRIES + 1):
        try:
            (handle,) = earthaccess.open([granule])
            with h5py.File(handle, "r") as h:
                return [layer(h, s, p) for s, p in layers]
        except SkillError:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == _READ_TRIES:
                raise DataError(
                    f"reading the {dataset} granule for {day_iso} failed {_READ_TRIES} times ({exc})."
                ) from exc
            time.sleep(5 * attempt)


def _fetch_nasa(dataset, days, bbox, layers, recommended_only):
    """The EASE-2 9 km box's latitudes, longitudes, and {day: one field (or None) per layer}."""
    lat, lon, rows, cols = _ease9_box(bbox)
    _login()
    granules = _nasa_granules(dataset, days[0], days[-1])
    _note(f"no {_NASA[dataset][0]} file (left empty)", [d for d in days if d not in granules])
    fields = {d: [None] * len(layers) for d in days}

    def one(day):
        return _read_nasa(
            granules[day], dataset, layers, rows, cols, recommended_only, day.isoformat()
        )

    with ThreadPoolExecutor(_WORKERS) as pool:
        for day, read in zip(sorted(granules), pool.map(one, sorted(granules)), strict=True):
            fields[day] = read
    return lat, lon, fields


# --- CATDS -------------------------------------------------------------------------

_CATDS_BASE = "https://data.catds.fr/cpdc/Land_products/GRIDDED/L3SM"
_CATDS_LAST_RE07 = dt.date(2021, 5, 24)
# SMOS crosses the equator ascending at 06:00 and descending at 18:00 local time.
_CATDS_PRODUCT = {"AM": "MIR_CLF31A", "PM": "MIR_CLF31D"}
# CATDS files declare no valid range and hold values from 0 to 1; keep SPL3SMP_E's.
_CATDS_VALID = (0.02, 0.5)
_CATDS_ACK = (
    'These data were obtained from the "Centre Aval de Traitement des Données SMOS" (CATDS), '
    'operated for the "Centre National d\'Etudes Spatiales" (CNES, France) by IFREMER '
    "(Brest, France)"
)


def _catds_listing(session, url, pattern):
    resp = session.get(url, timeout=60)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return sorted(set(re.findall(pattern, resp.text)))


def _catds_day(session, day, overpass, bbox, workdir):
    """The day's CATDS soil moisture over ``bbox`` on its 25 km grid, or None when CATDS has no file."""
    import requests
    import xarray as xr

    archive = "RE07" if day <= _CATDS_LAST_RE07 else "OPER"
    url = f"{_CATDS_BASE}/{archive}/{_CATDS_PRODUCT[overpass]}/{day.year}/{day.timetuple().tm_yday:03d}/"
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
        da = raw["Soil_Moisture"].rename(lat="latitude", lon="longitude").sortby("latitude")
        da = ensure_normalized_longitude(da)
        if bbox is not None:
            da = bbox_subset(da.to_dataset(), bbox, lat_dim="latitude", lon_dim="longitude")[
                "Soil_Moisture"
            ]
        lo, hi = _CATDS_VALID
        da = da.where((da >= lo) & (da <= hi)).astype("float32").load()
    path.unlink()
    return da


def _catds_latest(overpasses):
    """The latest day CATDS has every overpass for."""
    import requests

    session, latest = requests.Session(), None
    try:
        for p in overpasses:
            top = f"{_CATDS_BASE}/OPER/{_CATDS_PRODUCT[p]}/"
            year = _catds_listing(session, top, r'href="(\d{4})/?"')[-1]
            doy = _catds_listing(session, f"{top}{year}/", r'href="(\d{3})/?"')[-1]
            day = dt.date(int(year), 1, 1) + dt.timedelta(days=int(doy) - 1)
            latest = day if latest is None else min(latest, day)
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"could not list CATDS ({exc}).") from None
    return latest


def _fetch_catds(days, bbox, layers):
    """The SMOS 25 km box's latitudes, longitudes, and {day: one field (or None) per layer}."""
    import requests

    n = len(days) * len(layers)
    if n > _WARN_DAYS:
        _note(
            f"warning: {n} CATDS files is about {7 * n // 1000} GB of global files; expect a long download."
        )
    session = requests.Session()
    jobs = [(d, i, p) for d in days for i, (_, p) in enumerate(layers)]
    with tempfile.TemporaryDirectory(prefix=f"{_NAME}-") as tmp:
        with ThreadPoolExecutor(_WORKERS) as pool:
            got = list(pool.map(lambda j: _catds_day(session, j[0], j[2], bbox, tmp), jobs))
    for p in dict.fromkeys(p for _, p in layers):
        _note(
            f"no CATDS {_CATDS_PRODUCT[p]} file (left empty)",
            [d for (d, _, q), da in zip(jobs, got, strict=True) if q == p and da is None],
        )
    found = [da for da in got if da is not None]
    if not found:
        raise DataError(f"CATDS has no SMOS files for {days[0]}..{days[-1]}.")
    lat, lon = found[0]["latitude"].values, found[0]["longitude"].values
    fields = {d: [None] * len(layers) for d in days}
    for (d, i, _), da in zip(jobs, got, strict=True):
        if da is not None:
            fields[d][i] = da.reindex(latitude=lat, longitude=lon).values
    return lat, lon, fields


# --- radiometers (smap, smos, smap-smos) -------------------------------------------


def _fetch_radiometer(dataset, days, bbox, overpasses, recommended_only):
    """Daily soil moisture for ``days``, the first of each day's layers with a value per cell."""
    import numpy as np
    import xarray as xr

    sensors = ("smap", "smos") if dataset == "smap-smos" else (dataset,)
    # Fill order: AM before PM, SMAP before SMOS. soil_moisture_source is the 1-based place here.
    layers = [(s, p) for p in overpasses for s in sensors]

    if dataset == "smos":
        lat, lon, fields = _fetch_catds(days, bbox, layers)
    else:
        lat, lon, fields = _fetch_nasa(dataset, days, bbox, layers, recommended_only)

    shape = (len(days), lat.size, lon.size)
    sm = np.full(shape, np.nan, dtype="float32")
    flag = np.zeros(shape, dtype="int8")
    for t, day in enumerate(days):
        for k, field in enumerate(fields[day], start=1):
            if field is not None:
                fill = np.isnan(sm[t]) & ~np.isnan(field)
                sm[t][fill] = field[fill]
                flag[t][fill] = k

    dims = ("time", "latitude", "longitude")
    passes = "AM and PM overpasses" if len(overpasses) == 2 else f"{overpasses[0]} overpass"
    ds = xr.Dataset(
        {
            "soil_moisture": (
                dims,
                sm,
                {
                    "long_name": f"surface soil moisture ({dataset}, {passes})",
                    "comment": "Top ~5 cm. EASE-Grid 2.0 cell centres: latitude spacing varies.",
                    **_VOLUMETRIC,
                },
            ),
        },
        coords={"time": np.array(days, dtype="datetime64[ns]"), "latitude": lat, "longitude": lon},
    )
    if len(layers) > 1:
        ds["soil_moisture_source"] = xr.DataArray(
            flag,
            dims=dims,
            attrs={
                "long_name": "retrieval and overpass each soil_moisture value came from",
                "flag_values": np.arange(len(layers) + 1, dtype="int8"),
                "flag_meanings": " ".join(["none"] + [f"{s}_{p.lower()}" for s, p in layers]),
            },
        )
    ds.attrs["source"] = _DATASETS[dataset]["title"] + f", {passes}"
    if dataset == "smos":
        ds.attrs["acknowledgement"] = _CATDS_ACK
    return ds


@weather_skill(name=_NAME, version=_SKILL_VERSION)
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--dataset",
    choices=("ascat", "combined", "smap", "smos", "smap-smos"),
    required=True,
    help=(
        "ascat (C3S ACTIVE, % saturation), combined (C3S COMBINED), smap (SPL3SMP_E), "
        "smos (CATDS L3), or smap-smos (NSIDC-0800: SMAP gaps filled from SMOS retrieved the SMAP way)."
    ),
)
@weather_skill.argument(
    "--overpass",
    choices=("AM", "PM", "both"),
    help="smap, smos, smap-smos only: AM = 6am (default), PM = 6pm local; both = AM, gaps filled from PM.",
)
@weather_skill.argument(
    "--quality",
    choices=("recommended", "all"),
    help="smap, smap-smos only: recommended (default) keeps only retrievals flagged recommended quality.",
)
@weather_skill.argument(
    "--uncertainty",
    action="store_true",
    help="ascat, combined only: also write the uncertainty variable.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help="Print the latest YYYY-MM-DD --dataset (and --overpass) has (or none) and exit.",
)
def fetch(
    start_time, end_time, bbox, dataset, overpass, quality, uncertainty, probe_latest, **kwargs
):
    """Fetch daily satellite surface soil moisture (ASCAT, C3S COMBINED, SMAP, SMOS, or SMAP+SMOS) as a weather-skills Zarr."""
    import numpy as np

    cds = dataset in _CDS_VARS
    if cds and overpass:
        raise UsageError(f"--dataset {dataset} merges each day's passes; drop --overpass.")
    if quality and dataset not in _NASA:
        raise UsageError(f"--quality applies to smap and smap-smos, not {dataset}.")
    if uncertainty and not cds:
        raise UsageError(f"--uncertainty applies to ascat and combined, not {dataset}.")
    overpasses = ("AM", "PM") if overpass == "both" else (overpass or "AM",)

    # Every Earthdata call (login, search, open) sits in here, so a 401/403 from
    # any of them gets the login hint.
    try:
        if probe_latest is not None:
            today = dt.datetime.now(dt.UTC).date()
            if cds:
                latest = max(_cds_days(dataset))
            elif dataset in _NASA:
                _login()
                latest = max(
                    _nasa_granules(dataset, today - dt.timedelta(days=180), today), default=None
                )
            else:
                latest = _catds_latest(overpasses)
            print(latest.isoformat() if latest else "none")
            return None

        if start_time is None or end_time is None:
            raise UsageError("pass --start-time and --end-time (YYYY-MM-DD).")
        if end_time < start_time:
            raise UsageError("--end-time is before --start-time.")
        info = _DATASETS[dataset]
        if start_time < info["first"]:
            raise UsageError(f"--dataset {dataset} starts {info['first'].isoformat()}.")
        days = [start_time + dt.timedelta(days=i) for i in range((end_time - start_time).days + 1)]
        _note(f"{dataset} {start_time}..{end_time}: {len(days)} day(s)")
        if cds and len(days) > _WARN_DAYS:
            _note(
                f"warning: {len(days)} days is about {len(days)} MB of global files; expect a long download."
            )

        if cds:
            ds = _fetch_cds(dataset, days, bbox, uncertainty)
            # Every requested day is in the output; days the CDS lacks are NaN.
            ds = ds.reindex(time=np.array(days, dtype="datetime64[ns]"))
        else:
            ds = _fetch_radiometer(dataset, days, bbox, overpasses, quality != "all")
            ds = ensure_normalized_longitude(ds.sortby("latitude"), lon_dim="longitude")
    except Exception as exc:
        cause = exc.__cause__ or exc
        status = getattr(getattr(cause, "response", None), "status_code", None)
        status = status or getattr(cause, "status_code", None)
        text = str(cause).lower()
        if dataset in _NASA and (
            status in (401, 403) or "401 unauthorized" in text or "403 forbidden" in text
        ):
            raise DataError(_AUTH_FAIL_MSG) from None
        raise

    var = _CDS_VARS[dataset][2] if cds else "soil_moisture"
    empty = [
        d
        for d, e in zip(days, ds[var].isnull().all(("latitude", "longitude")).values, strict=True)
        if e
    ]
    if len(empty) == len(days):
        raise DataError(
            f"no usable {dataset} retrievals in the requested area within {start_time}..{end_time}; "
            "try --overpass both, another --dataset, or a wider window."
        )
    _note("no usable retrievals in the area (left empty)", empty)

    ds.attrs = {
        "Conventions": "CF-1.13",
        "title": info["title"],
        "institution": info["institution"],
        "references": info["references"],
        "weather_skills_source": info["source_tag"],
        **ds.attrs,
    }
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

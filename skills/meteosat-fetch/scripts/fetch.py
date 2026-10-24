# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "cftime",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "eumdac",
#   "satpy",
#   "pyresample",
#   "netCDF4",
#   "h5netcdf",
#   "dask[array]",
# ]
# ///
"""Fetch Meteosat SEVIRI or FCI visible and infrared scans over a bbox from the EUMETSAT Data Store."""

# Each scan is downloaded to a temporary directory, calibrated with satpy
# (visible -> reflectance %, infrared -> brightness temperature K), cropped and
# nearest-neighbour resampled from the geostationary projection onto a regular
# lat/lon grid at native sampling, and the raw files are deleted before the next
# scan. Credentials: EUMETSAT_CONSUMER_KEY + EUMETSAT_CONSUMER_SECRET, or the
# `key,secret` file eumdac writes at ~/.eumdac/credentials.

import math
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import is_transient

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# (instrument, service) -> (Data Store collection id, sub-satellite longitude)
_SOURCES = {
    ("seviri", "0deg"): ("EO:EUM:DAT:MSG:HRSEVIRI", 0.0),
    ("seviri", "iodc"): ("EO:EUM:DAT:MSG:HRSEVIRI-IODC", 45.5),
    ("fci", "0deg"): ("EO:EUM:DAT:0662", 0.0),
}
_READERS = {"seviri": "seviri_l1b_native", "fci": "fci_l1c_nc"}
_CADENCE_MIN = {"seviri": 15, "fci": 10}
# First day FCI L1c (EO:EUM:DAT:0662) has data in the Data Store (checked 2026-10).
_FCI_FIRST_DAY = date(2024, 9, 24)

# Shortcut groups. "vis" = solar-reflective channels (calibrated to reflectance),
# "ir" = thermal channels incl. water vapour (brightness temperature). SEVIRI HRV
# is left out: different grid and only a partial disk.
_BANDS = {
    "seviri": {
        "vis": ("VIS006", "VIS008", "IR_016"),
        "ir": ("IR_039", "WV_062", "WV_073", "IR_087", "IR_097", "IR_108", "IR_120", "IR_134"),
    },
    "fci": {
        "vis": ("vis_04", "vis_05", "vis_06", "vis_08", "vis_09", "nir_13", "nir_16", "nir_22"),
        "ir": ("ir_38", "wv_63", "wv_73", "ir_87", "ir_97", "ir_105", "ir_123", "ir_133"),
    },
}

# Request-size guards: this skill is for a handful of views, not time series.
_WARN_SCANS = 4
_DEFAULT_MAX_SCANS = 12
_WARN_DOWNLOAD_MB = 1500

# Geostationary geometry (WGS84, PROJ `geos` with sweep=y, as satpy uses for
# both SEVIRI and FCI) and the FCI L1c chunk layout: 40 equal-height BODY
# chunks spanning y in [-5568 km, +5568 km], numbered from the south.
_A = 6378137.0
_B = 6356752.314245
_H = 35786400.0
_FCI_N_CHUNKS = 40
_FCI_Y_HALF = 5568000.0


def _with_retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1 or not is_transient(exc):
                raise
            print(f"meteosat-fetch: transient error ({exc}); retrying", file=sys.stderr)


def _geos_y(lat, lon, sub_lon):
    """PROJ geos (sweep=y) forward y in metres, or None when not visible."""
    es = 1.0 - (_B / _A) ** 2
    radius_g = 1.0 + _H / _A
    phi = math.atan((1.0 - es) * math.tan(math.radians(lat)))
    lam = math.radians(lon - sub_lon)
    r = math.sqrt(1.0 - es) / math.hypot(math.sqrt(1.0 - es) * math.cos(phi), math.sin(phi))
    vx = r * math.cos(lam) * math.cos(phi)
    vy = r * math.sin(lam) * math.cos(phi)
    vz = r * math.sin(phi)
    tmp = radius_g - vx
    if tmp * vx - vy * vy - vz * vz / (1.0 - es) < 0:
        return None
    return _H * math.atan(vz / math.hypot(vy, tmp))


def _download(product, entries, workdir, workers):
    def one(entry):
        dst = workdir / Path(entry).name

        def go():
            with product.open(entry=entry) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out, length=8 * 1024 * 1024)

        _with_retry(go)
        return dst

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, entries))


def _process_scan(files, instrument, bands, bbox):
    """Calibrate, crop, and resample one scan onto a lat/lon grid. Returns (lat, lon, {band: 2-D}, attrs)."""
    import dask
    import numpy as np
    from pyresample import create_area_def
    from satpy import Scene

    # Keep `full` referenced until compute: its file handlers own the open
    # netCDF files, and if it is garbage-collected the lazy arrays in the
    # cropped/resampled scenes fail with "NetCDF: Not a valid ID".
    full = Scene(reader=_READERS[instrument], filenames=[str(f) for f in files])
    full.load(bands)
    north, west, south, east = bbox
    try:
        scn = full.crop(ll_bbox=(west, south, east, north))
    except Exception as exc:
        raise DataError(f"bbox is not on the {instrument} disk: {exc}") from exc

    # Native sampling (metres at the sub-satellite point) of the finest requested
    # band -> degrees, e.g. FCI 1 km -> 0.01, SEVIRI 3 km -> 0.03. The bbox snaps
    # outward to that grid, so cell centres sit at (k + 0.5) * res.
    pixel_m = float(min(abs(scn[b].attrs["area"].pixel_size_x) for b in bands))
    res = round(pixel_m / 1000.0) / 100.0
    extent = (
        math.floor(round(west / res, 6)) * res,
        math.floor(round(south / res, 6)) * res,
        math.ceil(round(east / res, 6)) * res,
        math.ceil(round(north / res, 6)) * res,
    )
    area = create_area_def("bbox", "EPSG:4326", area_extent=extent, resolution=res, units="degrees")
    out = scn.resample(area, resampler="nearest", radius_of_influence=4 * pixel_m)
    # 1-D cell centres from the area extent, rows north to south.
    lon = extent[0] + (np.arange(area.width) + 0.5) * area.pixel_size_x
    lat = extent[3] - (np.arange(area.height) + 0.5) * area.pixel_size_y
    computed = dask.compute(*(out[b].data for b in bands))
    values = {b: np.asarray(v, dtype="float32") for b, v in zip(bands, computed, strict=True)}
    attrs = {
        b: {k: scn[b].attrs.get(k) for k in ("units", "standard_name", "wavelength")} for b in bands
    }
    del out, scn, full
    return lat.astype("float64"), lon.astype("float64"), values, attrs


@weather_skill(name="meteosat-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--date", required=True)
@weather_skill.argument(
    "--time",
    action="append",
    required=True,
    metavar="HH:MM",
    help=(
        "UTC scan time (repeatable, or comma-separated). Floored to the repeat cycle "
        "start: 15 min for SEVIRI, 10 min for FCI. Keep this to a few views."
    ),
)
@weather_skill.argument(
    "--bbox",
    required=True,
    help="N/W/S/E region to keep (required; whole disks are never stored).",
)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    required=True,
    help=(
        "Band(s) to return (repeatable, or comma-separated): `vis` (all solar "
        "channels), `ir` (all thermal channels), or explicit names, e.g. "
        "SEVIRI VIS006,IR_108 or FCI vis_06,ir_105."
    ),
)
@weather_skill.argument(
    "--service",
    choices=("0deg", "iodc"),
    default="0deg",
    help="0deg (Meteosat prime, default) or iodc (Indian Ocean, 45.5E; SEVIRI only).",
)
@weather_skill.argument(
    "--instrument",
    choices=("seviri", "fci"),
    default=None,
    help=("Default: fci for 0deg on/after 2024-09-24, seviri otherwise (and always for iodc)."),
)
@weather_skill.argument(
    "--max-scans",
    type=int,
    default=_DEFAULT_MAX_SCANS,
    help=f"Refuse requests above this many scans (default {_DEFAULT_MAX_SCANS}).",
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=4,
    help="Concurrent file downloads per scan (default 4).",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the day of the latest scan (YYYY-MM-DD) for --service/--instrument "
        "and exit; stderr gives its HH:MM UTC. Downloads nothing. IDENT is unused."
    ),
)
def fetch(date, time, bbox, variable, service, instrument, max_scans, workers, **kwargs):
    """Fetch Meteosat SEVIRI or FCI visible and infrared scans over a bbox from the EUMETSAT Data Store."""
    import eumdac

    probe = kwargs.get("probe_latest") is not None
    # FCI by default, but SEVIRI for IODC and for days before FCI L1c starts.
    if instrument is None:
        instrument = (
            "seviri" if service == "iodc" or (not probe and date < _FCI_FIRST_DAY) else "fci"
        )
    if (instrument, service) not in _SOURCES:
        raise UsageError(
            f"--instrument {instrument} has no --service {service}: IODC is SEVIRI-only."
        )
    collection_id, sub_lon = _SOURCES[(instrument, service)]
    cadence = timedelta(minutes=_CADENCE_MIN[instrument])

    key = os.environ.get("EUMETSAT_CONSUMER_KEY")
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET")
    path = Path("~/.eumdac/credentials").expanduser()
    if not (key and secret) and path.is_file():
        key, _, secret = path.read_text().strip().partition(",")
    if not (key and secret):
        raise UsageError(
            "no EUMETSAT credentials: set EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET, "
            "or run `eumdac set-credentials KEY SECRET` to write ~/.eumdac/credentials."
        )
    token = eumdac.AccessToken((key, secret))

    if probe:
        # The newest scan in the last 3 hours, day, or week, floored to its repeat cycle.
        collection = eumdac.DataStore(token).get_collection(collection_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        for hours in (3, 24, 24 * 7):
            prods = list(collection.search(dtstart=now - timedelta(hours=hours), dtend=now))
            if prods:
                latest = max(p.sensing_start for p in prods)
                latest -= (latest - datetime.min) % cadence
                print(f"{latest:%Y-%m-%d}")
                print(f"meteosat-fetch: latest scan {latest:%Y-%m-%d %H:%M} UTC", file=sys.stderr)
                return None
        raise DataError(f"no {instrument} {service} scans in the last week.")

    import numpy as np
    import pandas as pd
    import xarray as xr

    if instrument == "fci" and date < _FCI_FIRST_DAY:
        raise UsageError(
            f"FCI L1c starts {_FCI_FIRST_DAY.isoformat()}; use --instrument seviri for {date}."
        )

    # Bands: `vis`/`ir` shortcuts or explicit names, matched case-insensitively.
    groups = _BANDS[instrument]
    valid = {b.lower(): b for b in groups["vis"] + groups["ir"]}
    bands = []
    for token in (t.strip() for item in variable for t in item.split(",")):
        if not token:
            continue
        names = groups.get(token.lower()) or [valid.get(token.lower())]
        if None in names:
            raise UsageError(
                f"unknown {instrument} band {token!r}. Use vis, ir, or one of: "
                + ", ".join(valid.values())
            )
        bands.extend(n for n in names if n not in bands)
    if not bands:
        raise UsageError("pass -v vis, -v ir, or explicit band names.")

    # Times, each floored to its repeat cycle start (the scan's nominal time).
    requested = set()
    for token in (t.strip() for item in time for t in item.split(",")):
        if not token:
            continue
        try:
            requested.add(datetime.combine(date, dt_time.fromisoformat(token)))
        except ValueError:
            raise UsageError(f"--time {token!r} is not HH:MM (UTC).") from None
    if not requested:
        raise UsageError("pass at least one --time HH:MM (UTC).")
    slots = sorted({t - (t - datetime.min) % cadence for t in requested})
    if len(slots) < len(requested):
        print(
            f"meteosat-fetch: {len(requested)} --time values fall in {len(slots)} "
            f"{instrument} repeat cycle(s) ({_CADENCE_MIN[instrument]} min); "
            "fetching each cycle once.",
            file=sys.stderr,
        )
    if len(slots) > max_scans:
        raise UsageError(
            f"{len(slots)} scans requested; this skill is for a few views "
            f"(--max-scans {max_scans}). Raise --max-scans if you really need more."
        )
    if len(slots) > _WARN_SCANS:
        print(
            f"meteosat-fetch: warning: {len(slots)} scans requested; each is a "
            "large download -- consider fewer views.",
            file=sys.stderr,
        )

    # The bbox's geostationary y range, from a 21 x 21 grid of its points.
    north, west, south, east = bbox
    ys = [
        _geos_y(south + (north - south) * i / 20, west + (east - west) * j / 20, sub_lon)
        for i in range(21)
        for j in range(21)
    ]
    ys = [y for y in ys if y is not None]
    if not ys:
        raise UsageError(f"--bbox is not visible from the {service} satellite.")
    # FCI: the BODY chunks (1 = southernmost) covering that range, one chunk margin.
    chunks = []
    if instrument == "fci":
        height = 2 * _FCI_Y_HALF / _FCI_N_CHUNKS
        lo = max(1, int((min(ys) + _FCI_Y_HALF) // height))
        hi = min(_FCI_N_CHUNKS, int((max(ys) + _FCI_Y_HALF) // height) + 2)
        chunks = list(range(lo, hi + 1))

    # One product per slot: the scan that starts in that repeat cycle.
    collection = eumdac.DataStore(token).get_collection(collection_id)
    products = {}
    for slot in slots:
        hits = [
            p
            for p in _with_retry(
                lambda s=slot: list(collection.search(dtstart=s, dtend=s + cadence))
            )
            if slot <= p.sensing_start < slot + cadence
        ]
        if not hits:
            raise DataError(
                f"no {instrument} scan for {slot:%Y-%m-%d %H:%M} UTC in {collection}; "
                "check --probe-latest or try a neighbouring time."
            )
        products[slot] = min(hits, key=lambda p: p.sensing_start)

    # Product.size is in KB for the whole disk; FCI fetches only some chunks.
    frac = len(chunks) / _FCI_N_CHUNKS if instrument == "fci" else 1.0
    est_mb = sum((p.size or 0) for p in products.values()) / 1024 * frac
    print(
        f"meteosat-fetch: {instrument} {service} {collection_id}: {len(slots)} scan(s), "
        f"bands {','.join(bands)}, ~{est_mb:,.0f} MB to download"
        + (f" (FCI chunks {chunks[0]}-{chunks[-1]})" if chunks else ""),
        file=sys.stderr,
    )
    if est_mb > _WARN_DOWNLOAD_MB:
        print(
            f"meteosat-fetch: warning: ~{est_mb / 1024:.1f} GB download; "
            "consider fewer --time values.",
            file=sys.stderr,
        )

    stacks = {b: [] for b in bands}
    scan_start = []
    platform = set()
    lat = lon = attrs = None
    for slot, product in products.items():
        print(f"meteosat-fetch: {slot:%Y-%m-%d %H:%M} <- {product}", file=sys.stderr)
        # SEVIRI: the one .nat file. FCI: the BODY chunks, named ..._NNNN.nc.
        if instrument == "seviri":
            entries = [e for e in product.entries if e.endswith(".nat")]
        else:
            entries = [
                e
                for e in product.entries
                if "-CHK-BODY-" in e
                and e.endswith(".nc")
                and int(Path(e).stem.rsplit("_", 1)[-1]) in chunks
            ]
        if not entries:
            raise DataError(f"{product} has no usable files for this bbox.")
        with tempfile.TemporaryDirectory(prefix="meteosat-fetch-") as tmp:
            files = _download(product, entries, Path(tmp), workers)
            lat, lon, values, attrs = _process_scan(files, instrument, bands, bbox)
        for b in bands:
            stacks[b].append(values[b])
        scan_start.append(product.sensing_start)
        platform.add(str(getattr(product, "satellite", "") or ""))

    times = pd.DatetimeIndex(list(products))
    out = xr.Dataset(
        {b: (("time", "latitude", "longitude"), np.stack(stacks[b])) for b in bands},
        coords={
            "time": times,
            "latitude": lat,
            "longitude": lon,
            "scan_start": ("time", pd.DatetimeIndex(scan_start)),
        },
    )
    out["scan_start"].attrs["long_name"] = "sensing start of the repeat cycle"
    for b in bands:
        a = attrs[b]
        reflective = b in _BANDS[instrument]["vis"]
        out[b].attrs = {
            "units": a.get("units") or ("%" if reflective else "K"),
            "standard_name": a.get("standard_name")
            or ("toa_bidirectional_reflectance" if reflective else "toa_brightness_temperature"),
            "long_name": f"{instrument.upper()} {b} "
            + ("reflectance" if reflective else "brightness temperature"),
        }
        wl = a.get("wavelength")
        if wl is not None:
            out[b].attrs["central_wavelength_um"] = float(wl.central)
        empty = float(np.isnan(out[b].values).mean())
        if empty > 0.5:
            print(
                f"meteosat-fetch: warning: {b} is {empty:.0%} missing over the bbox "
                "(off-disk or outside the downloaded area).",
                file=sys.stderr,
            )

    out.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"eumetsat-datastore:{collection_id}",
        platform=",".join(sorted(p for p in platform if p)),
        instrument=instrument.upper(),
        eumetsat_service=service,
        sub_satellite_longitude=sub_lon,
    )
    stamp_cf_attrs(out)
    return out


if __name__ == "__main__":
    fetch()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
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
"""Fetch Meteosat SEVIRI / FCI visible and infrared imagery from the EUMETSAT Data Store for a bbox.

Each requested scan is downloaded to a temporary directory, calibrated with satpy
(visible -> reflectance %, infrared -> brightness temperature K), cropped and
nearest-neighbour resampled from the satellite's geostationary projection onto a
regular lat/lon grid at the instrument's native sampling, and the raw files are
deleted before the next scan. Only the bbox is ever kept.

Credentials: EUMETSAT_CONSUMER_KEY + EUMETSAT_CONSUMER_SECRET, or the
``key,secret`` file eumdac writes at ~/.eumdac/credentials.
"""

import math
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import is_transient

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

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


def _credentials():
    key = os.environ.get("EUMETSAT_CONSUMER_KEY")
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET")
    if key and secret:
        return key, secret
    path = Path("~/.eumdac/credentials").expanduser()
    if path.is_file():
        parts = path.read_text().strip().split(",")
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]
    raise UsageError(
        "no EUMETSAT credentials: set EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET, "
        "or run `eumdac set-credentials KEY SECRET` to write ~/.eumdac/credentials."
    )


def _resolve_instrument(instrument, service, day):
    if instrument is None:
        instrument = "seviri" if service == "iodc" or day < _FCI_FIRST_DAY else "fci"
    if (instrument, service) not in _SOURCES:
        raise UsageError(
            f"--instrument {instrument} has no --service {service}: IODC is SEVIRI-only."
        )
    if instrument == "fci" and day < _FCI_FIRST_DAY:
        raise UsageError(
            f"FCI L1c starts {_FCI_FIRST_DAY.isoformat()}; use --instrument seviri for {day}."
        )
    return instrument


def _resolve_bands(requested, instrument):
    """Expand `vis`/`ir` shortcuts and match explicit names case-insensitively."""
    groups = _BANDS[instrument]
    valid = groups["vis"] + groups["ir"]
    by_lower = {b.lower(): b for b in valid}
    out = []
    for item in requested:
        for token in (t.strip() for t in item.split(",")):
            if not token:
                continue
            names = groups.get(token.lower()) or (
                (by_lower[token.lower()],) if token.lower() in by_lower else None
            )
            if names is None:
                raise UsageError(
                    f"unknown {instrument} band {token!r}. Use vis, ir, or one of: "
                    + ", ".join(valid)
                )
            out.extend(n for n in names if n not in out)
    if not out:
        raise UsageError("pass --band vis, --band ir, or explicit band names.")
    return out


def _parse_times(day, values):
    out = []
    for value in values:
        for token in (t.strip() for t in value.split(",")):
            if not token:
                continue
            try:
                t = time.fromisoformat(token)
            except ValueError:
                raise UsageError(f"--time {token!r} is not HH:MM (UTC).") from None
            dt = datetime.combine(day, t)
            if dt not in out:
                out.append(dt)
    if not out:
        raise UsageError("pass at least one --time HH:MM (UTC).")
    return sorted(out)


def _slot(dt, instrument):
    """Floor to the instrument's repeat-cycle start (the scan's nominal time)."""
    cadence = _CADENCE_MIN[instrument]
    return dt.replace(minute=dt.minute - dt.minute % cadence, second=0, microsecond=0)


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


def _visible_y_range(bbox, sub_lon, n=21):
    north, west, south, east = bbox
    ys = []
    for i in range(n):
        lat = south + (north - south) * i / (n - 1)
        for j in range(n):
            y = _geos_y(lat, west + (east - west) * j / (n - 1), sub_lon)
            if y is not None:
                ys.append(y)
    if not ys:
        return None
    return min(ys), max(ys)


def _fci_chunks(bbox, sub_lon):
    """BODY chunk numbers (1 = southernmost) covering bbox, with one chunk margin."""
    yr = _visible_y_range(bbox, sub_lon)
    if yr is None:
        return []
    height = 2 * _FCI_Y_HALF / _FCI_N_CHUNKS

    def idx(y):
        return int((y + _FCI_Y_HALF) // height) + 1

    lo = max(1, idx(yr[0]) - 1)
    hi = min(_FCI_N_CHUNKS, idx(yr[1]) + 1)
    return list(range(lo, hi + 1))


def _chunk_number(entry):
    return int(Path(entry).stem.rsplit("_", 1)[-1])


def _entries_to_download(product_entries, instrument, chunks):
    if instrument == "seviri":
        return [e for e in product_entries if e.endswith(".nat")]
    wanted = set(chunks)
    return [
        e
        for e in product_entries
        if "-CHK-BODY-" in e and e.endswith(".nc") and _chunk_number(e) in wanted
    ]


def _grid(bbox, res):
    """Snap bbox outward to a res-aligned grid; cell centres at (k + 0.5) * res."""
    north, west, south, east = bbox
    w = math.floor(round(west / res, 6)) * res
    e = math.ceil(round(east / res, 6)) * res
    s = math.floor(round(south / res, 6)) * res
    n = math.ceil(round(north / res, 6)) * res
    return w, s, e, n


def _with_retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1 or not is_transient(exc):
                raise
            print(f"meteosat-fetch: transient error ({exc}); retrying", file=sys.stderr)


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


def _find_products(collection, slots, instrument):
    """One product per nominal slot (the scan that starts in that repeat cycle)."""
    cadence = timedelta(minutes=_CADENCE_MIN[instrument])
    found = {}
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
        found[slot] = min(hits, key=lambda p: p.sensing_start)
    return found


def _process_scan(files, instrument, bands, bbox):
    """Calibrate, crop, and resample one scan onto a lat/lon grid. Returns (lat, lon, {band: 2-D})."""
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
    # band -> degrees, e.g. FCI 1 km -> 0.01, SEVIRI 3 km -> 0.03.
    pixel_m = float(min(abs(scn[b].attrs["area"].pixel_size_x) for b in bands))
    res = round(pixel_m / 1000.0) / 100.0
    w, s, e, n = _grid(bbox, res)
    area = create_area_def(
        "bbox",
        "EPSG:4326",
        area_extent=(w, s, e, n),
        resolution=res,
        units="degrees",
    )
    out = scn.resample(area, resampler="nearest", radius_of_influence=4 * pixel_m)
    lons, lats = area.get_lonlats()
    computed = dask.compute(*(out[b].data for b in bands))
    values = {b: np.asarray(v, dtype="float32") for b, v in zip(bands, computed, strict=True)}
    attrs = {
        b: {k: scn[b].attrs.get(k) for k in ("units", "standard_name", "wavelength")} for b in bands
    }
    del out, scn, full
    return lats[:, 0].astype("float64"), lons[0, :].astype("float64"), values, attrs


def _latest(collection, instrument):
    now = datetime.now(UTC).replace(tzinfo=None)
    for hours in (3, 24, 24 * 7):
        prods = list(collection.search(dtstart=now - timedelta(hours=hours), dtend=now))
        if prods:
            return _slot(max(p.sensing_start for p in prods), instrument)
    return None


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
    "--band",
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
        "Print the latest available scan as `YYYY-MM-DD HH:MM` (UTC) for "
        "--service/--instrument and exit. Downloads nothing. IDENT is unused."
    ),
)
def fetch(date, time, bbox, band, service, instrument, max_scans, workers, **kwargs):
    """Fetch Meteosat SEVIRI/FCI VIS and IR imagery for a bbox from the EUMETSAT Data Store."""
    import eumdac

    if kwargs.get("probe_latest") is not None:
        instrument = instrument or ("seviri" if service == "iodc" else "fci")
        if (instrument, service) not in _SOURCES:
            raise UsageError("IODC is SEVIRI-only.")
        token = eumdac.AccessToken(_credentials())
        coll = eumdac.DataStore(token).get_collection(_SOURCES[(instrument, service)][0])
        latest = _latest(coll, instrument)
        if latest is None:
            raise DataError(f"no {instrument} {service} scans in the last week.")
        print(f"{latest:%Y-%m-%d %H:%M}")
        return None

    import numpy as np
    import pandas as pd
    import xarray as xr

    instrument = _resolve_instrument(instrument, service, date)
    bands = _resolve_bands(band, instrument)
    requested = _parse_times(date, time)
    slots = sorted({_slot(t, instrument) for t in requested})
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

    collection_id, sub_lon = _SOURCES[(instrument, service)]
    chunks = []
    if instrument == "fci":
        chunks = _fci_chunks(bbox, sub_lon)
    if _visible_y_range(bbox, sub_lon) is None:
        raise UsageError(f"--bbox is not visible from the {service} satellite.")

    token = eumdac.AccessToken(_credentials())
    collection = eumdac.DataStore(token).get_collection(collection_id)
    products = _find_products(collection, slots, instrument)

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
        entries = _entries_to_download(list(product.entries), instrument, chunks)
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

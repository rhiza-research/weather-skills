# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "zarr>=3",
#   "gcsfs",
# ]
# ///
"""Fetch Google PARDIG 15-minute precipitation estimates as a weather-skills Zarr."""

import sys

from weather_skills_core import DataError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# Time-series layout: each chunk is the full time axis of one 0.5° x 0.5° tile,
# so a small box over many years reads few chunks.
_STORES = {
    "nrt": "gs://pardig_1/NRT/zarr_rechunked_as_timeseries/2016_to_2025/predictions.zarr",
    "lr": "gs://pardig_1/LR/zarr_rechunked_as_timeseries/1999_to_2023/predictions.zarr",
}
# NRT from 2026 on: one global Zarr per 15-minute frame, named <YYYYMMDDTHHMMSS>.
_LIVE = "gs://pardig_1/NRT/zarr/2026_to_present"
_LIVE_START = "2026-01-01"


def _live_frames():
    """{valid time: frame name} for every published live NRT frame."""
    import gcsfs
    import numpy as np

    fs = gcsfs.GCSFileSystem(token="anon")
    # fs.ls also lists the prefix itself; keep only <YYYYMMDDTHHMMSS> frame names.
    names = [p.rsplit("/", 1)[-1] for p in fs.ls(_LIVE)]
    return {
        np.datetime64(f"{s[:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}"): s
        for s in names
        if len(s) == 15 and s[8] == "T"
    }


def _read_live(first, last, bbox):
    """Box of every published live NRT frame in [first, last], or None if there are none."""
    from concurrent.futures import ThreadPoolExecutor

    import xarray as xr

    stamps = [s for t, s in sorted(_live_frames().items()) if first <= t <= last]
    if not stamps:
        return None
    print(f"pardig-fetch: reading {len(stamps)} live NRT frames", file=sys.stderr)

    def one(stamp):
        frame = xr.open_zarr(
            f"{_LIVE}/{stamp}/predictions.zarr", storage_options={"token": "anon"}, chunks=None
        )
        frame = bbox_subset(
            ensure_normalized_longitude(frame), bbox, lat_dim="latitude", lon_dim="longitude"
        )
        return frame[["precipitation"]].load()

    # Each frame is one global chunk (~1.2 MB), so read many at once.
    with ThreadPoolExecutor(32) as pool:
        return xr.concat(list(pool.map(one, stamps)), dim="time")


def _open_store(product):
    import xarray as xr

    store = _STORES[product]
    try:
        return xr.open_zarr(store, storage_options={"token": "anon"}, chunks=None)
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"could not open PARDIG store {store} ({type(exc).__name__}: {exc}). "
            "Check network access to Google Cloud Storage; the bucket is public."
        ) from None


@weather_skill(name="pardig-fetch", version=_SKILL_VERSION)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--bbox",
    required=True,
    help="N/W/S/E bounding box.",
)
@weather_skill.argument(
    "--product",
    choices=["nrt", "lr"],
    default="nrt",
    help="nrt: PARDIG-NRT 2016 to present (default). lr: PARDIG-LR 1999-2023.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the day of the newest frame of --product (YYYY-MM-DD) on stdout "
        "and exit; the frame time goes to stderr. Downloads no data. IDENT is ignored."
    ),
)
def fetch(start_time, end_time, bbox, product, probe_latest, **kwargs):
    """Fetch Google PARDIG 15-minute precipitation estimates as a weather-skills Zarr."""
    import numpy as np
    import xarray as xr

    if probe_latest is not None:
        times = list(_live_frames()) if product == "nrt" else []
        newest = max(times) if times else _open_store(product)["time"].values[-1]
        print(
            f"pardig-fetch: newest PARDIG-{product.upper()} frame {str(newest)[:16]}",
            file=sys.stderr,
        )
        print(str(newest)[:10])
        return

    first = np.datetime64(start_time.isoformat())
    last = np.datetime64(end_time.isoformat()) + np.timedelta64(1, "D") - np.timedelta64(1, "m")
    live_start = np.datetime64(_LIVE_START)

    parts = []
    if product == "lr" or first < live_start:
        ds = _open_store(product)
        ds = bbox_subset(
            ensure_normalized_longitude(ds), bbox, lat_dim="latitude", lon_dim="longitude"
        )
        ds = ds.sel(time=slice(first, last))
        if ds.sizes["time"]:
            print(
                f"pardig-fetch: reading PARDIG-{product.upper()} archive, "
                f"{ds.sizes['latitude']}x{ds.sizes['longitude']} cells, {ds.sizes['time']} frames",
                file=sys.stderr,
            )
            parts.append(ds[["precipitation"]])
    if product == "nrt" and last >= live_start:
        live = _read_live(max(first, live_start), last, bbox)
        if live is not None:
            parts.append(live)
    if not parts:
        raise DataError(
            f"PARDIG-{product.upper()} has no data in {start_time}..{end_time}. "
            "NRT covers 2016-01-01T07:45 to a few hours ago, LR 1999..2023."
        )
    try:
        out = xr.concat(parts, dim="time").rename(precipitation="precip").compute()
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed reading PARDIG ({type(exc).__name__}: {exc}).") from None

    # Frames the source lacks (outside the product period, not yet published) stay absent, not filled.
    expected = int((last - first) // np.timedelta64(15, "m")) + 1
    if out.sizes["time"] < expected:
        print(
            f"WARNING: PARDIG-{product.upper()} has {out.sizes['time']} of {expected} 15-minute "
            f"frames in the window ({str(out['time'].values[0])[:16]}.."
            f"{str(out['time'].values[-1])[:16]}); missing frames are absent, not filled.",
            file=sys.stderr,
        )

    out["precip"].attrs = {"units": "m h-1", "long_name": "precipitation rate"}  # source "m h**-1"
    out = to_standard_units(out, variables=["precip"])
    out.attrs = {
        "Conventions": "CF-1.13",
        "title": f"PARDIG-{product.upper()} precipitation {start_time}..{end_time}",
        "institution": "Google DeepMind / Google Research",
        "source": _STORES[product] if product == "lr" else f"{_STORES['nrt']}, {_LIVE}",
        "references": "https://developers.google.com/weathernext/guides/pardig",
        "license": "CC BY 4.0",
        "weather_skills_source": f"pardig-{product}",
    }
    stamp_cf_attrs(out)
    return stamp_data_interval(out, period="15 minute")


if __name__ == "__main__":
    fetch()

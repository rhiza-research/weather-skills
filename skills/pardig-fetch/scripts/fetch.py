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
    help="N/W/S/E bounding box. Every 0.5° tile it touches downloads its whole time axis.",
)
@weather_skill.argument(
    "--product",
    choices=["nrt", "lr"],
    default="nrt",
    help="nrt: PARDIG-NRT 2016-2025 (default). lr: PARDIG-LR 1999-2023.",
)
def fetch(start_time, end_time, bbox, product, **kwargs):
    """Fetch Google PARDIG 15-minute precipitation estimates as a weather-skills Zarr."""
    import numpy as np

    ds = _open_store(product)
    ds = bbox_subset(ensure_normalized_longitude(ds), bbox, lat_dim="latitude", lon_dim="longitude")

    first = np.datetime64(start_time.isoformat())
    last = np.datetime64(end_time.isoformat()) + np.timedelta64(1, "D") - np.timedelta64(1, "m")
    ds = ds.sel(time=slice(first, last))
    if ds.sizes["time"] == 0:
        raise DataError(
            f"PARDIG-{product.upper()} has no data in {start_time}..{end_time}. "
            "NRT covers 2016-01-01T07:45..2025-12-31, LR 1999..2023 "
            "(2026 frames are not in the time-series stores)."
        )

    print(
        f"pardig-fetch: reading PARDIG-{product.upper()} {start_time}..{end_time}, "
        f"{ds.sizes['latitude']}x{ds.sizes['longitude']} cells, {ds.sizes['time']} frames",
        file=sys.stderr,
    )
    try:
        out = ds[["precipitation"]].rename(precipitation="precip").compute()
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed reading the PARDIG store ({type(exc).__name__}: {exc}).") from None

    # Requested days the store does not reach are absent rows, not filled.
    got_first, got_last = out["time"].values[[0, -1]]
    if got_first > first or got_last < last - np.timedelta64(14, "m"):
        print(
            f"WARNING: PARDIG-{product.upper()} covers only {str(got_first)[:16]}.."
            f"{str(got_last)[:16]} of the requested window.",
            file=sys.stderr,
        )

    out["precip"].attrs = {"units": "m h-1", "long_name": "precipitation rate"}  # source "m h**-1"
    out = to_standard_units(out, variables=["precip"])
    out.attrs = {
        "Conventions": "CF-1.13",
        "title": f"PARDIG-{product.upper()} precipitation {start_time}..{end_time}",
        "institution": "Google DeepMind / Google Research",
        "source": _STORES[product],
        "references": "https://developers.google.com/weathernext/guides/pardig",
        "license": "CC BY 4.0",
        "weather_skills_source": f"pardig-{product}",
    }
    stamp_cf_attrs(out)
    return stamp_data_interval(out, period="15 minute")


if __name__ == "__main__":
    fetch()

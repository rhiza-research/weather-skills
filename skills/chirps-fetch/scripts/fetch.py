# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@old-dev",
#   "cftime",
#   "dynamical-catalog==1.0.1",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch CHIRPS precipitation from the dynamical.org catalog (final, prelim fallback) and write a weather-skills standard dataset Zarr."""

import os
import sys
from datetime import date, timedelta

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    np_to_date,
)
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_FINAL_ID = "ucsb-chc-chirps-analysis-final"
_PRELIM_ID = "ucsb-chc-chirps-analysis-preliminary"
_STAGING_CATALOG = "https://stac-staging.dynamical.org/catalog.json"
_VAR = "precipitation_surface"
_DROP_COORDS = ("spatial_ref",)
CHIRPS_START_YEAR = 1981
DEFAULT_WORKERS = 8


def _use_staging_catalog() -> None:
    import dynamical_catalog

    os.environ["DYNAMICAL_STAC_CATALOG_URL"] = _STAGING_CATALOG
    dynamical_catalog.clear_cache()


def _ensure_catalog() -> None:
    """Prefer the production catalog; fall through to staging until CHIRPS is promoted."""
    import dynamical_catalog

    if _FINAL_ID in dynamical_catalog.list():
        return
    _use_staging_catalog()
    ids = dynamical_catalog.list()
    if _FINAL_ID not in ids:
        raise DataError(
            f"{_FINAL_ID} is not in the dynamical.org catalog "
            f"(tried production and staging). Available: {', '.join(ids)}"
        )


def _open_catalog(dataset_id: str):
    import dynamical_catalog

    _ensure_catalog()
    return dynamical_catalog.open(dataset_id, chunks=None)


def _present_dates(ds) -> list[date]:
    return [np_to_date(t) for t in ds["time"].values]


def _subset(ds, start_time: date, end_time: date, bbox):
    import numpy as np

    if bbox is not None:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
    else:
        ds = ensure_normalized_longitude(ds, lon_dim="longitude")
    ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords or c in ds])
    return ds.sel(
        time=slice(np.datetime64(start_time.isoformat()), np.datetime64(end_time.isoformat()))
    )


def _select_dates(ds, days: list[date]):
    import numpy as np

    return ds.sel(time=[np.datetime64(day.isoformat()) for day in days])


def _latest_day(dataset_id: str) -> date | None:
    import numpy as np

    try:
        ds = _open_catalog(dataset_id)
    except Exception:
        return None
    if ds.sizes.get("time", 0) == 0:
        return None
    return np_to_date(np.max(ds["time"].values))


@weather_skill(
    name="chirps-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help="Ignored; retained for CLI compatibility with the previous TIF fetcher.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(start_time, end_time, bbox, **kwargs):
    """Fetch CHIRPS precipitation from the dynamical.org catalog (final, prelim fallback) and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        found = [day for day in (_latest_day(_FINAL_ID), _latest_day(_PRELIM_ID)) if day is not None]
        if not found:
            raise DataError("CHIRPS probe found no time coordinate on the dynamical.org catalogs")
        print(max(found).isoformat())
        return

    import xarray as xr

    start = start_time.isoformat()
    end = end_time.isoformat()
    region = f" bbox={bbox[0]:g}/{bbox[1]:g}/{bbox[2]:g}/{bbox[3]:g}" if bbox is not None else ""
    print(
        f"Fetching CHIRPS {start} -> {end} from dynamical.org "
        f"({_FINAL_ID}, {_PRELIM_ID} fallback){region}",
        file=sys.stderr,
    )

    expected_days = [
        start_time + timedelta(days=i) for i in range((end_time - start_time).days + 1)
    ]

    final = _subset(_open_catalog(_FINAL_ID), start_time, end_time, bbox)
    if _VAR not in final:
        raise DataError(f"{_FINAL_ID} has no {_VAR} variable.")
    final_dates = set(_present_dates(final))
    missing = [day for day in expected_days if day not in final_dates]

    fill_dates: list[date] = []
    if missing:
        prelim = _subset(_open_catalog(_PRELIM_ID), start_time, end_time, bbox)
        if _VAR in prelim:
            prelim_dates = set(_present_dates(prelim))
            fill_dates = [day for day in missing if day in prelim_dates]
            missing = [day for day in missing if day not in prelim_dates]
        else:
            prelim = None
    else:
        prelim = None

    pieces = []
    if final_dates:
        pieces.append(final[[_VAR]])
    if fill_dates and prelim is not None:
        pieces.append(_select_dates(prelim[[_VAR]], fill_dates))

    if not pieces:
        raise UsageError(
            f"no days available in range {start}..{end} from the dynamical.org "
            f"CHIRPS catalogs ({_FINAL_ID} / {_PRELIM_ID}). Coverage starts in "
            f"{CHIRPS_START_YEAR}; the validated final product lags the calendar "
            "month, and very recent days come from the preliminary product "
            "(published 2 days after each pentad closes — pentads end on days "
            "5, 10, 15, 20, 25, and last of month, worst-case lag ~7 days)."
        )

    ds = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
    ds = ds.sortby("time")
    if "latitude" in ds.dims:
        ds = ds.sortby("latitude")

    succeeded_days = _present_dates(ds)
    last_succeeded = succeeded_days[-1]
    expected_tail = [day for day in expected_days if day > last_succeeded]
    if missing and missing != expected_tail:
        raise UsageError(
            f"Non-tail missing day(s) {', '.join(d.isoformat() for d in missing)} "
            f"— server-side data gap, not a lag issue. "
            "Refusing to write a partial zarr with a hole in the middle.",
            prefix=False,
        )

    if missing:
        print(
            f"Tail-missing day(s) {', '.join(d.isoformat() for d in missing)}; "
            f"writing partial dataset with effective end {last_succeeded.isoformat()} "
            f"(requested --end-time was {end}). "
            "Consistent with CHIRPS v3.0 preliminary's pentad-based schedule.",
            file=sys.stderr,
        )

    ds = to_standard_units(ds, variables=[_VAR])
    ds = ds.rename({_VAR: "precip"})
    ds["precip"].attrs.setdefault("long_name", "CHIRPS daily precipitation")
    ds.attrs.update(Conventions="CF-1.13", weather_skills_source="chirps")
    stamp_cf_attrs(ds)
    ds = ensure_normalized_longitude(ds, lon_dim="longitude")
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

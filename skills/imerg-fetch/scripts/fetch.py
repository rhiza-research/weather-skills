# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.6",
#   "dynamical-catalog==1.0.1",
#   "earthaccess",
#   "h5netcdf",
#   "h5py",
# ]
# ///
"""Fetch NASA IMERG precipitation (Early, Late or Final) as a weather-skills Zarr."""

import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    np_to_date,
)
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.8"

VERSIONS = ["early", "late", "final"]
# dynamical.org half-hourly stores: no login, read only the bbox.
CATALOG_IDS = {
    "early": "nasa-imerg-analysis-early",
    "late": "nasa-imerg-analysis-late",
}
# NASA GES DISC daily files: Earthdata login, one global file per day.
SHORTNAMES = {
    "late": "GPM_3IMERGDL",
    "final": "GPM_3IMERGDF",
}
_STAGING_CATALOG = "https://stac-staging.dynamical.org/catalog.json"
_GRANULE_DATE_RE = re.compile(r"\.(\d{8})-S")
_AUTH_FAIL_MSG = (
    "Earthdata authentication failed; configure EARTHDATA_USERNAME/"
    "EARTHDATA_PASSWORD or a urs.earthdata.nasa.gov entry in ~/.netrc, then retry."
)


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
        if getattr(auth, "authenticated", False):
            return
    raise DataError(_AUTH_FAIL_MSG)


def _open_catalog(dataset):
    """Open a dynamical.org store, retrying on the staging catalog if production lacks it."""
    import dynamical_catalog

    catalog = dynamical_catalog.list()
    if dataset not in catalog:
        os.environ["DYNAMICAL_STAC_CATALOG_URL"] = _STAGING_CATALOG
        dynamical_catalog.clear_cache()
        catalog = dynamical_catalog.list()
    if dataset not in catalog:
        raise DataError(f"{dataset} is not in the dynamical.org catalog (production or staging)")
    return dynamical_catalog.open(dataset)


@weather_skill(
    name="imerg-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--version", default="late", choices=VERSIONS)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(IMERG early, late or final)."
    ),
)
def fetch(start_time, end_time, version, bbox, probe_latest, **kwargs):
    """Fetch NASA IMERG precipitation (Early, Late or Final) as a weather-skills Zarr."""
    if probe_latest is not None:
        import numpy as np

        release = probe_latest or version
        if release not in VERSIONS:
            raise UsageError(f"unknown IMERG product {release!r}; choose early, late or final")
        if release in CATALOG_IDS:
            latest = np.max(_open_catalog(CATALOG_IDS[release])["time"].values)
            if latest != latest.astype("datetime64[D]"):
                print(
                    f"Note: latest timestamp is {str(latest)[:16]}; that day is partial.",
                    file=sys.stderr,
                )
            print(np_to_date(latest).isoformat())
            return
        import earthaccess

        _login()
        # Newest granule first: Final can lag (or stop) far behind any fixed window.
        results = earthaccess.search_data(
            short_name=SHORTNAMES[release],
            cloud_hosted=True,
            sort_key="-start_date",
            count=1,
        )
        days = []
        for granule in results:
            for url in granule.data_links():
                match = _GRANULE_DATE_RE.search(url.rsplit("/", 1)[-1])
                if match:
                    ymd = match.group(1)
                    days.append(date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])))
                    break
        if not days:
            raise DataError(f"no IMERG {release} granules found")
        print(max(days).isoformat())
        return

    import earthaccess
    import numpy as np
    import xarray as xr

    start = start_time.isoformat()
    end = end_time.isoformat()

    # Early and Late try dynamical.org first; Late falls back to GES DISC daily files.
    fallback = None
    if version in CATALOG_IDS:
        dataset = CATALOG_IDS[version]
        lo = np.datetime64(start)
        hi = np.datetime64(end) + np.timedelta64(1, "D")
        try:
            ds = _open_catalog(dataset)
            times = ds["time"].values
        except Exception as exc:  # noqa: BLE001 — any catalog failure triggers the fallback
            fallback = f"could not open dynamical.org {dataset} ({exc})"
        else:
            inside = (times >= lo) & (times < hi)
            if not inside.any():
                fallback = f"dynamical.org {dataset} has no data in {start}..{end}"
        if fallback and version == "early":
            raise DataError(f"{fallback}; IMERG Early has no other source.")

    if version in CATALOG_IDS and fallback is None:
        print(
            f"Fetching IMERG {version} from dynamical:{dataset} {start} -> {end}", file=sys.stderr
        )
        # Keep every half-hourly step of the end day. The catalog lists only
        # published times, so a window past the latest time comes back shorter.
        # Say so; never pad.
        spacing = times[1] - times[0] if len(times) > 1 else np.timedelta64(1, "D")
        if times[inside][0] > lo or times[inside][-1] + spacing < hi:
            print(
                f"WARNING: {dataset} covers only {str(times[inside][0])[:16]}.."
                f"{str(times[inside][-1])[:16]} of the requested {start}..{end}; "
                "writing that partial window (not padded).",
                file=sys.stderr,
            )
        ds = ds[["precipitation_surface"]].isel(time=np.nonzero(inside)[0])
        ds = ds.drop_vars([c for c in ("spatial_ref",) if c in ds.coords])
        if bbox is not None:
            ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
        else:
            ds = ensure_normalized_longitude(ds, lon_dim="longitude")
        ds = ds.rename({"precipitation_surface": "precip"}).sortby("latitude")
        ds.attrs.update(
            Conventions="CF-1.13",
            weather_skills_source=f"dynamical:{dataset}",
            source=f"dynamical.org {dataset} (IMERG {version.capitalize()} half-hourly, 0.1 degree)",
            imerg_version=version,
        )
        stamp_cf_attrs(ds)
        ds = to_standard_units(ds, variables=["precip"])
        return stamp_data_interval(ds)

    if fallback:
        print(
            f"NOTE: {fallback}; falling back to NASA GES DISC GPM_3IMERGDL. "
            "Output is DAILY (data_interval 1 day), not half-hourly.",
            file=sys.stderr,
        )
    shortname = SHORTNAMES[version]

    print(f"Fetching IMERG {version} ({shortname}) {start} -> {end}", file=sys.stderr)

    _login()
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(start, end),
    )
    if not results:
        raise DataError(
            f"no IMERG {version} granules found in {start}..{end}; "
            f"run --probe-latest {version} for the latest published day"
        )
    print(f"Found {len(results)} granules", file=sys.stderr)

    requested_span = (end_time - start_time).days + 1

    pieces = []
    with tempfile.TemporaryDirectory(prefix="imerg-fetch-") as td:
        # One granule at a time: download, cut to --bbox, delete. Holding every
        # global file (~32 MB a day) at once filled the disk on multi-season windows.
        for granule in results:
            for path in earthaccess.download([granule], local_path=td):
                with xr.open_dataset(path, engine="h5netcdf") as day:
                    day = day[["precipitation"]].rename({"precipitation": "precip"})
                    day = day.rename({"lat": "latitude", "lon": "longitude"})
                    if bbox is not None:
                        north, west, south, east = bbox
                        day = day.sel(latitude=slice(south, north))
                        # W > E crosses the antimeridian: two contiguous slices.
                        lon_slices = [slice(west, east)]
                        if west > east:
                            lon_slices = [slice(west, None), slice(None, east)]
                        day = xr.concat([day.sel(longitude=s) for s in lon_slices], dim="longitude")
                    pieces.append(day.load())
                Path(path).unlink(missing_ok=True)
    ds = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
    src_units = ds["precip"].attrs.get("units")
    if src_units != "mm/day":
        raise DataError(f"unexpected IMERG precipitation units {src_units!r}; expected 'mm/day'")
    if ds.sizes["latitude"] == 0 or ds.sizes["longitude"] == 0:
        raise DataError(f"--bbox {bbox} selects no IMERG grid cells; check the N/W/S/E order")
    ds = ds.sel(time=slice(start, end))
    present_days = sorted({np.datetime64(t, "D").item() for t in ds["time"].values})
    present_days = [d for d in present_days if start_time <= d <= end_time]
    covered_days = len(present_days)
    if covered_days == 0:
        raise DataError(f"no granule day falls within {start}..{end}")
    if covered_days < requested_span:
        last_present = present_days[-1]
        expected_tail = [
            start_time + timedelta(days=i)
            for i in range(requested_span)
            if start_time + timedelta(days=i) > last_present
        ]
        missing_days = sorted(
            {start_time + timedelta(days=i) for i in range(requested_span)} - set(present_days)
        )
        if missing_days != expected_tail:
            raise DataError(
                f"non-trailing missing day(s) "
                f"{', '.join(d.isoformat() for d in missing_days)} within "
                f"{start}..{end} — server/data gap, not lag. Refusing to write "
                "a partial zarr with a hole in the middle."
            )
        print(
            f"WARNING: requested {requested_span} days ({start}..{end}) but only "
            f"{covered_days} distinct day(s) are present in that span; writing the "
            f"available days through {last_present.isoformat()} (trailing days not yet "
            "published, or near the dataset start).",
            file=sys.stderr,
        )

    # GES DISC daily files are (time, lon, lat); the standard order is (time, lat, lon).
    ds = ds.transpose("time", "latitude", "longitude").load()
    ds = ds.drop_attrs()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="imerg",
        source=f"NASA GES DISC {shortname} (IMERG {version.capitalize()} daily, 0.1 degree)",
        imerg_version=version,
    )
    ds["precip"].attrs.update(
        units="mm day-1",
        standard_name="lwe_precipitation_rate",
        long_name=f"IMERG {version.capitalize()} daily mean precipitation rate",
    )
    stamp_cf_attrs(ds)
    ds = ensure_normalized_longitude(ds, lon_dim="longitude")
    ds = to_standard_units(ds, variables=["precip"])
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

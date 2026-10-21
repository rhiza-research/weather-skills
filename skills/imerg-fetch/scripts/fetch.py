# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "earthaccess",
#   "h5netcdf",
#   "h5py",
#   "dask",
# ]
# ///
"""Fetch NASA IMERG daily precipitation from GES DISC (Earthdata login) as a weather-skills Zarr."""

import re
import sys
import tempfile
from datetime import date, timedelta

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

SHORTNAMES = {
    "late": "GPM_3IMERGDL",
    "final": "GPM_3IMERGDF",
}
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


@weather_skill(
    name="imerg-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--version", default="late", choices=list(SHORTNAMES))
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(start_time, end_time, version, probe_latest, **kwargs):
    """Fetch NASA IMERG daily precipitation from GES DISC (Earthdata login) as a weather-skills Zarr."""
    if probe_latest is not None:
        import earthaccess

        release = probe_latest or version or "late"
        if release not in SHORTNAMES:
            raise UsageError(f"unknown IMERG product {release!r}; choose late or final")
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

    shortname = SHORTNAMES[version]
    start = start_time.isoformat()
    end = end_time.isoformat()

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

    with tempfile.TemporaryDirectory(prefix="imerg-fetch-") as td:
        files = earthaccess.download(results, local_path=td)
        ds = xr.open_mfdataset(files, engine="h5netcdf", combine="by_coords")
        ds = ds[["precipitation"]].rename({"precipitation": "precip"})
        src_units = ds["precip"].attrs.get("units")
        if src_units != "mm/day":
            raise DataError(
                f"unexpected IMERG precipitation units {src_units!r}; expected 'mm/day'"
            )
        spatial_rename = {
            src: dst
            for src, dst in {"lat": "latitude", "lon": "longitude"}.items()
            if src in ds.dims
        }
        if spatial_rename:
            ds = ds.rename(spatial_rename)
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

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@old-dev",
#   "xarray",
#   "zarr",
#   "numpy",
#   "netCDF4",
#   "cf_xarray",
#   "pint-xarray>=0.6",
#   "cftime",
# ]
# ///
"""Fetch NOAA OISST v2.1 daily sea-surface temperature from NOAA PSL OPeNDAP and write a weather-skills standard dataset Zarr."""

import sys
import time
from datetime import UTC, datetime

import cf_xarray  # noqa: F401  (fail-fast probe; core loads it lazily at write time)
from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_coords
from weather_skills_core.standard_utils import (
    apply_write_encoding,
    bbox_subset,
    ensure_normalized_longitude,
    is_transient,
    np_to_date,
    verify_cf_decode,
)
from weather_skills_core.units import convert_dataarray, stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_OPENDAP_URL = (
    "https://psl.noaa.gov/thredds/dodsC/Datasets/noaa.oisst.v2.highres/sst.day.mean.{year}.nc"
)

_CF_GLOBAL_ATTRS = {
    "Conventions": "CF-1.13",
    "title": (
        "NOAA/NCEI 1/4 Degree Daily Optimum Interpolation Sea Surface Temperature "
        "(OISST) Analysis, Version 2.1"
    ),
    "source": (
        "NOAA OISST v2.1 daily analysis, read from NOAA PSL OPeNDAP (sst.day.mean.<year>.nc)"
    ),
    "institution": "NOAA/National Centers for Environmental Information",
    "references": (
        "https://www.psl.noaa.gov/data/gridded/data.noaa.oisst.v2.highres.html ; "
        "Huang et al. 2021, https://doi.org/10.1175/JCLI-D-20-0166.1"
    ),
    "history": (
        "oisst-fetch: subset NOAA OISST v2.1 daily SST from NOAA PSL OPeNDAP "
        "to the resolved time window and bounding box"
    ),
}

_SST_STANDARD_NAME = "sea_surface_temperature"
_SST_LONG_NAME = "Daily Sea Surface Temperature"

# Source attrs that describe the pre-subset global extent or contradict NaN fill.
_STALE_RANGE_ATTRS = (
    "actual_range",
    "valid_range",
    "_ChunkSizes",
    "missing_value",
    "valid_min",
    "valid_max",
)

_TIME_UNITS = "days since 1970-01-01 00:00:00"
_TIME_CALENDAR = "standard"
_RETRY_SLEEP_S = 2.0


def _strip_dangling_bounds(ds):
    """Remove a `bounds` attr when the named bounds variable is absent."""
    present = set(ds.variables)
    for name in ds.coords:
        bnds = ds[name].attrs.get("bounds")
        if bnds is not None and bnds not in present:
            del ds[name].attrs["bounds"]
    return ds


def _stamp_cf(ds):
    """Stamp CF-1.13 attrs; convert sst to degree_Celsius."""
    import cf_xarray.units  # noqa: F401
    from pint import application_registry as ureg

    ds.attrs.update(_CF_GLOBAL_ATTRS)
    stamp_cf_coords(
        ds, long_names={"latitude": "Latitude", "longitude": "Longitude", "time": "Time"}
    )

    src_units = ds["sst"].attrs.get("units")
    try:
        unit = ureg.Unit(src_units)
        valid = bool(src_units) and unit.is_compatible_with(ureg.Unit("K"))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise DataError(
            f"source OISST sst units {src_units!r} are not a temperature "
            "unit convertible to K; refusing to stamp CF "
            f"standard_name={_SST_STANDARD_NAME!r}."
        )
    converted, _ = convert_dataarray(ds["sst"], "degree_Celsius")
    ds["sst"] = converted
    ds["sst"].attrs["standard_name"] = _SST_STANDARD_NAME
    ds["sst"].attrs["long_name"] = _SST_LONG_NAME
    ds["sst"].attrs["units"] = "degree_Celsius"

    for name in ("sst", "latitude", "longitude", "time"):
        if name in ds.variables:
            for attr in _STALE_RANGE_ATTRS:
                ds[name].attrs.pop(attr, None)

    return _strip_dangling_bounds(ds)


def _is_availability_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        m in text for m in ("not found", "no such file", "404", "does not exist", "file not found")
    )


def _looks_like_html_error(exc: Exception) -> bool:
    """NOAA PSL sometimes serves a 502 HTML page in place of OPeNDAP bytes."""
    text = str(exc).lower()
    return any(m in text for m in ("doctype", "proxy error", "<html", "502 proxy"))


def _is_retryable(exc: Exception) -> bool:
    if _is_availability_failure(exc):
        return False
    return is_transient(exc) or _looks_like_html_error(exc) or _is_undecoded_time(exc)


def _is_undecoded_time(exc: Exception) -> bool:
    text = str(exc).lower()
    return "isnat" in text or "not a datetime" in text or "error page instead of data" in text


def _is_transport_failure(exc: Exception) -> bool:
    if _is_availability_failure(exc) or _is_retryable(exc):
        return False
    text = str(exc).lower()
    return any(
        m in text
        for m in ("dap failure", "dap2", "dap", "curl", "connection", "timed out", "timeout")
    )


def _latest_day(ds):
    """Return the latest time as a date, or raise if the axis is not datetime.

    A 502 HTML error page can make netCDF4 print a syntax warning and still
    return a dataset whose ``time`` values are numeric or strings. ``np_to_date``
    then crashes on ``np.isnat``; treat that as a failed open instead.
    """
    import numpy as np

    if "time" not in ds:
        raise DataError("OISST file has no time coordinate")
    values = np.asarray(ds["time"].values)
    if values.size == 0:
        raise DataError("OISST file has an empty time coordinate")
    latest = np.max(values)
    try:
        return np_to_date(latest)
    except (TypeError, ValueError) as exc:
        raise DataError(
            "OISST time coordinate is not a datetime; NOAA PSL may have returned "
            f"an error page instead of data ({type(latest).__name__})"
        ) from exc


def _open_year(year: int):
    """Open one yearly OISST OPeNDAP file."""
    import xarray as xr

    return xr.open_dataset(_OPENDAP_URL.format(year=year))


def _probe_year(year: int):
    """Latest published day in ``year``, retrying once on a 502/HTML-as-dataset."""
    last_exc = None
    for attempt in range(2):
        ds = None
        try:
            ds = _open_year(year)
            latest = _latest_day(ds)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if ds is not None:
                try:
                    ds.close()
                except Exception:  # noqa: BLE001
                    pass
            if attempt == 0 and _is_retryable(exc):
                print(
                    f"OISST OPeNDAP transient error probing {year} ({exc}); retrying once",
                    file=sys.stderr,
                )
                time.sleep(_RETRY_SLEEP_S)
                continue
            raise
        ds.close()
        return latest
    raise last_exc


@weather_skill(
    name="oisst-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
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
    """Fetch NOAA OISST v2.1 daily sea-surface temperature from NOAA PSL OPeNDAP and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        today = datetime.now(UTC).date()
        years = [today.year]
        # Early January the current-year file may not exist yet.
        if today.month == 1:
            years.append(today.year - 1)
        last_exc = None
        for y in years:
            try:
                latest = _probe_year(y)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_availability_failure(exc) and today.year - 1 not in years:
                    years.append(today.year - 1)
                    continue
                # A 502 on the current year is not "use December 31 last year".
                break
            print(latest.isoformat())
            return
        raise DataError(
            "could not open a current or previous-year OISST file to probe latest time"
            + (f" ({last_exc})" if last_exc is not None else "")
        )

    import numpy as np
    import xarray as xr

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    time_slice = slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59"))
    years = list(range(start_time.year, end_time.year + 1))
    bbox_label = f"{bbox[0]}/{bbox[1]}/{bbox[2]}/{bbox[3]}" if bbox is not None else "global"
    print(
        f"Fetching oisst {start_iso}..{end_iso} (years {years[0]}..{years[-1]})",
        file=sys.stderr,
    )

    pieces = []
    for year in years:
        piece = None
        last_exc = None
        for attempt in range(2):
            try:
                # No dask chunking: chunked OPeNDAP reads were observed to write zeros.
                dy = _open_year(year)
                with dy:
                    piece = dy[["sst"]].rename({"lat": "latitude", "lon": "longitude"})
                    piece = ensure_normalized_longitude(piece)
                    piece = piece.sel(time=time_slice)
                    if bbox is not None:
                        piece = bbox_subset(piece, bbox, lat_dim="latitude", lon_dim="longitude")
                    piece = piece.load()
                break
            except SkillError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and _is_retryable(exc):
                    print(
                        f"OISST OPeNDAP transient error reading {year} ({exc}); retrying once",
                        file=sys.stderr,
                    )
                    time.sleep(_RETRY_SLEEP_S)
                    continue
                if _is_availability_failure(exc):
                    raise DataError(
                        f"could not open the OISST file for year {year} ({exc}). The year may be "
                        "outside the served range (1981-09 to present), or NOAA PSL's OPeNDAP server is "
                        "unreachable — check the date range."
                    ) from exc
                if _is_retryable(exc):
                    raise DataError(
                        f"NOAA PSL OPeNDAP was unreachable for year {year} ({exc}). "
                        "This is a transient server error; retry the request."
                    ) from exc
                if _is_transport_failure(exc):
                    raise UsageError(
                        f"OISST OPeNDAP rejected the data transfer for {start_iso}..{end_iso} "
                        f"bbox {bbox_label} (year {year}): {exc}. OISST is served "
                        "over NOAA PSL OPeNDAP, which limits request size; this request is too large. "
                        "Reduce --bbox and/or shorten the date range."
                    ) from exc
                raise UsageError(f"unexpected failure reading OISST year {year}: {exc}.") from exc
        if piece is None:
            raise DataError(
                f"could not open the OISST file for year {year} ({last_exc}). The year may be "
                "outside the served range (1981-09 to present), or NOAA PSL's OPeNDAP server is "
                "unreachable — check the date range."
            )
        if piece.sizes.get("time", 0) == 0:
            continue
        _stamp_cf(piece)
        pieces.append(piece)

    if not pieces:
        raise DataError(f"OISST has no data in {start_iso}..{end_iso}.")

    ds = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
    ds.attrs["weather_skills_source"] = "oisst"
    apply_write_encoding(
        ds,
        time_units=_TIME_UNITS,
        time_calendar=_TIME_CALENDAR,
        fills={"sst": np.float32("nan")},
    )
    verify_cf_decode(ds)
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()

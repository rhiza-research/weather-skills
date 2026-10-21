# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "netCDF4",
# ]
# ///
"""Fetch NOAA OISST v2.1 daily sea-surface temperature as a weather-skills Zarr."""

import sys
import time
from datetime import UTC, datetime

import cf_xarray  # noqa: F401  (fail-fast probe; core loads it lazily at write time)
from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_coords
from weather_skills_core.standard_utils import (
    apply_write_encoding,
    ensure_normalized_longitude,
    is_transient,
    np_to_date,
    verify_cf_decode,
)
from weather_skills_core.units import convert_dataarray, stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

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
_PRELIM_DAYS = 15
_PRELIM_NOTE = (
    f"Note: OISST days less than {_PRELIM_DAYS} days old are preliminary and may be revised; "
    "NOAA PSL serves them in the same file without a per-day flag."
)


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
    # "NetCDF: Access failure" (errno -77) is an intermittent NOAA PSL OPeNDAP
    # failure; the same request succeeds on retry.
    return (
        is_transient(exc)
        or _looks_like_html_error(exc)
        or _is_undecoded_time(exc)
        or "access failure" in str(exc).lower()
    )


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
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD on stdout and exit. "
        "Does not download fields. Takes no IDENT."
    ),
)
def fetch(start_time, end_time, bbox, probe_latest, **kwargs):
    """Fetch NOAA OISST v2.1 daily sea-surface temperature as a weather-skills Zarr."""
    today = datetime.now(UTC).date()
    if probe_latest is not None:
        if probe_latest:
            raise UsageError(
                f"--probe-latest takes no argument (got {probe_latest!r}); OISST has one "
                "product whose most recent days are preliminary."
            )
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
            print(_PRELIM_NOTE, file=sys.stderr)
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
    if (today - end_time).days < _PRELIM_DAYS:
        print(_PRELIM_NOTE, file=sys.stderr)

    pieces = []
    for year in years:
        piece = None
        last_exc = None
        for attempt in range(2):
            try:
                # No dask chunking: chunked OPeNDAP reads were observed to write zeros.
                dy = _open_year(year)
                with dy:
                    src = dy[["sst"]].rename({"lat": "latitude", "lon": "longitude"})
                    src = src.sel(time=time_slice)
                    # Read contiguous slices of the native 0..360 grid and wrap after
                    # loading: an out-of-order longitude indexer (a bbox crossing 0°)
                    # makes netCDF4 send one OPeNDAP request per longitude column.
                    lon_slices = [slice(None)]
                    if bbox is not None:
                        north, west, south, east = bbox
                        src = src.sel(latitude=slice(south, north))
                        if east - west < 360:
                            w, e = west % 360, east % 360
                            lon_slices = (
                                [slice(w, e)] if w <= e else [slice(w, None), slice(None, e)]
                            )
                    piece = xr.concat(
                        [src.sel(longitude=s).load() for s in lon_slices], dim="longitude"
                    )
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
                if _is_availability_failure(exc) and year >= today.year:
                    # Not published yet: the coverage note below reports the missing days.
                    break
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
            print(f"OISST file for {year} is not published yet; skipping it.", file=sys.stderr)
            continue
        if piece.sizes["latitude"] == 0 or piece.sizes["longitude"] == 0:
            raise DataError(
                f"--bbox {bbox_label} selects no OISST grid cells; check the extent and "
                "N/W/S/E order."
            )
        if piece.sizes.get("time", 0) == 0:
            continue
        piece = ensure_normalized_longitude(piece, lon_dim="longitude")
        _stamp_cf(piece)
        pieces.append(piece)

    if not pieces:
        raise DataError(f"OISST has no data in {start_iso}..{end_iso}.")

    ds = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
    requested = np.arange(
        np.datetime64(start_iso), np.datetime64(end_iso) + 1, dtype="datetime64[D]"
    )
    missing = np.setdiff1d(requested, ds["time"].values.astype("datetime64[D]"))
    if missing.size:
        print(
            f"Note: OISST has no data for {missing.size} of {requested.size} requested days "
            f"({missing[0]}..{missing[-1]}); they are absent from the output, not filled.",
            file=sys.stderr,
        )
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

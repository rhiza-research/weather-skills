# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "gcsfs",
# ]
# ///
"""Fetch ERA5 reanalysis from Google's public ARCO-ERA5 store as a weather-skills Zarr."""

import sys
from datetime import UTC, datetime

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.standard_utils import (
    apply_write_encoding,
    bbox_subset,
    ensure_normalized_longitude,
    np_to_date,
    verify_cf_decode,
)
from weather_skills_core.units import (
    classify_variable,
    precip_amounts_to_rates,
    stamp_data_interval,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

_ARCO_STORE = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
_STORAGE_OPTIONS = {"token": "anon"}
_ARCO_REFERENCE = "https://github.com/google-research/arco-era5"
_ARCO_INSTITUTION = "ECMWF (ERA5 reanalysis), republished as ARCO-ERA5 by Google Research"

# Same-unit relabels for ARCO `units` strings udunits won't parse. "~" is left
# alone (ERA5 placeholder for "no unit", not dimensionless) so the check rejects it.
_UNIT_FIXUPS = {
    "(0 - 1)": "1",
    "dimensionless": "1",
    "m of water equivalent": "m",
}

_CURATED_STANDARD_NAME = {
    "2m_temperature": "air_temperature",
    "2m_dewpoint_temperature": "dew_point_temperature",
    "10m_u_component_of_wind": "eastward_wind",
    "10m_v_component_of_wind": "northward_wind",
}


def _open_arco(state):
    """Open the ARCO store lazily (metadata only), at most once per run."""
    if "ds" not in state:
        import xarray as xr

        try:
            state["ds"] = xr.open_zarr(_ARCO_STORE, storage_options=_STORAGE_OPTIONS, chunks=None)
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"could not open the ARCO-ERA5 store at {_ARCO_STORE} "
                f"({type(exc).__name__}: {exc}). Check network access to Google Cloud "
                "Storage; the store is read anonymously, so no credentials are needed."
            ) from None
    return state["ds"]


def _fix_units(units):
    """Return a candidate CF unit string for an ARCO source `units` value, or None."""
    if not units:
        return None
    return _UNIT_FIXUPS.get(units, units)


def _udunits_valid(units: str) -> bool:
    import cf_xarray.units  # noqa: F401
    from pint import application_registry as ureg

    try:
        ureg.Unit(units)
    except Exception:  # noqa: BLE001
        return False
    return True


def _stamp_data_var_attrs(ds) -> None:
    """Stamp CF units/long_name/standard_name on every data var; validate units."""
    for name in ds.data_vars:
        src = ds[name].attrs
        raw_units = src.get("units")
        units = _fix_units(raw_units)
        if units is None:
            raise ValueError(
                f"data variable {name!r} has no source `units`; refusing to write it "
                "under Conventions=CF-1.13."
            )
        if not _udunits_valid(units):
            raise ValueError(
                f"data variable {name!r} has units {units!r} that are not udunits-valid "
                f"(source units were {raw_units!r}); refusing to write it under "
                "Conventions=CF-1.13."
            )
        long_name = src.get("long_name") or str(name)
        standard_name = src.get("standard_name") or _CURATED_STANDARD_NAME.get(name)
        new_attrs = {"units": units, "long_name": long_name}
        if standard_name:
            new_attrs["standard_name"] = standard_name
        ds[name].attrs = new_attrs


def _stamp_coord_attrs(ds) -> None:
    if "latitude" in ds.coords:
        ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    if "longitude" in ds.coords:
        ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    if "time" in ds.coords:
        ds["time"].attrs["standard_name"] = "time"
        ds["time"].attrs["axis"] = "T"
    if "level" in ds.coords:
        # Values are already hPa; relabel "Hectopascal(hPa)" -> "hPa".
        ds["level"].attrs.update(
            standard_name="air_pressure",
            units="hPa",
            positive="down",
            axis="Z",
            long_name="pressure level",
        )


@weather_skill(
    name="arco-era5-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
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
def fetch(start_time, end_time, bbox, variable, probe_latest, **kwargs):
    """Fetch ERA5 reanalysis from Google's public ARCO-ERA5 store as a weather-skills Zarr."""
    if probe_latest is not None:
        import numpy as np

        # The store pre-allocates its time axis through 2050, so max(time) is not
        # the data end. Start from the store's ERA5T stop attr and step back until
        # one 2m_temperature point at 23 UTC is finite (one chunk read per day).
        ds = _open_arco({})
        stop = ds.attrs.get("valid_time_stop_era5t") or ds.attrs.get("valid_time_stop")
        day = np.datetime64(stop or datetime.now(UTC).date().isoformat(), "D")
        for _ in range(60):
            hour = day + np.timedelta64(23, "h")
            point = ds["2m_temperature"].sel(time=hour).isel(latitude=0, longitude=0)
            if np.isfinite(point.values):
                break
            day -= np.timedelta64(1, "D")
        else:
            print("none")
            return
        if ds.attrs.get("valid_time_stop"):
            print(
                f"Note: days after {ds.attrs['valid_time_stop']} are preliminary ERA5T "
                "(final ERA5 replaces them ~2-3 months later).",
                file=sys.stderr,
            )
        print(np_to_date(day).isoformat())
        return

    import numpy as np

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()

    ds = _open_arco({})

    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(
                f"variable(s) not in ARCO-ERA5: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}"
            )
        ds = ds[variable]
    else:
        print(
            "Note: no --variable given; selecting all data variables (large). Pass -v to restrict.",
            file=sys.stderr,
        )

    ds = ensure_normalized_longitude(ds)
    if bbox:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")

    # ERA5 precip accumulations are labelled at the END of their hour (00 UTC
    # holds 23-24 UTC of the day before); the standard labels an interval at its
    # start. Read those variables one hour later and relabel them back, so each
    # day's 00..23 rows are that day's 24 hours. Other variables are instants.
    hour = np.timedelta64(1, "h")
    first = np.datetime64(f"{start_iso}T00:00")
    last = np.datetime64(f"{end_iso}T23:00")
    accum = [
        name
        for name in ds.data_vars
        if classify_variable(
            name,
            units=_fix_units(ds[name].attrs.get("units")),
            standard_name=ds[name].attrs.get("standard_name"),
        )
        == "precip_amount"
    ]
    names = list(ds.data_vars)
    window = ds.drop_vars(accum).sel(time=slice(first, last))
    if accum:
        import xarray as xr

        shifted = ds[accum].sel(time=slice(first + hour, last + hour))
        shifted = shifted.assign_coords(time=shifted["time"] - hour)
        window = xr.merge([window, shifted], compat="override", join="outer")[names]
    ds = window
    if ds.sizes.get("time", 0) == 0:
        raise DataError(f"ARCO-ERA5 has no data in {start_iso}..{end_iso}.")

    print(f"Fetching arco-era5 {start_iso}..{end_iso}", file=sys.stderr)

    stamped = datetime.now(UTC).isoformat(timespec="seconds")
    ds.attrs.clear()
    ds.attrs.update(
        Conventions="CF-1.13",
        title=f"ARCO-ERA5 reanalysis {start_iso}..{end_iso}",
        institution=_ARCO_INSTITUTION,
        source=_ARCO_STORE,
        references=_ARCO_REFERENCE,
        history=f"{stamped}: fetched by arco-era5-fetch {_SKILL_VERSION}",
        weather_skills_source="arco-era5",
    )
    _stamp_coord_attrs(ds)
    try:
        _stamp_data_var_attrs(ds)
    except ValueError as exc:
        raise DataError(str(exc)) from None

    verify_cf_decode(ds)
    if "level" in ds.coords:
        import cf_xarray  # noqa: F401

        if "Z" not in ds.cf.axes and "vertical" not in ds.cf.coordinates:
            raise DataError(
                "level present but did not resolve as a CF vertical coordinate. "
                "This is a bug in the fetcher's CF stamping, not a data problem."
            )

    try:
        ds = ds.compute()
    except MemoryError:
        raise DataError(
            "ran out of memory materializing the selection. "
            "Narrow it with -v, --bbox, or a shorter window."
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"failed while reading from the ARCO-ERA5 store ({type(exc).__name__}: {exc})."
        ) from None

    # Hours past the real data end are all-NaN (pre-allocated axis). Drop that
    # tail instead of writing empty rows; never fill.
    finite = np.zeros(ds.sizes["time"], dtype=bool)
    for name in ds.data_vars:
        if "time" in ds[name].dims:
            other = [d for d in ds[name].dims if d != "time"]
            finite |= ds[name].notnull().any(dim=other).values
    if not finite.any():
        raise DataError(
            f"ARCO-ERA5 has no data yet in {start_iso}..{end_iso} "
            "(the store's time axis runs ahead of the data). Run --probe-latest."
        )
    last = int(np.flatnonzero(finite)[-1])
    if last < ds.sizes["time"] - 1:
        print(
            f"Warning: ARCO-ERA5 data ends {str(ds['time'].values[last])[:13]}; dropped "
            f"{ds.sizes['time'] - 1 - last} all-NaN trailing hour(s). Effective end is "
            "earlier than --end-time.",
            file=sys.stderr,
        )
        ds = ds.isel(time=slice(0, last + 1))

    apply_write_encoding(
        ds,
        time_units=f"hours since {start_time.isoformat()} 00:00:00",
        time_calendar="proleptic_gregorian",
    )
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval="1 hour")
    _stamp_coord_attrs(ds)  # unit conversion respells level "hPa" as "hectopascal"
    return stamp_data_interval(ds, period="1 hour")


if __name__ == "__main__":
    fetch()

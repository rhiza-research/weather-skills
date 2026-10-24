# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "cftime>=1.6",
#   "netcdf4>=1.7",
# ]
# ///
"""Export a weather-skills Zarr to a NetCDF file, or read NetCDF files into a Zarr."""

import sys
from pathlib import Path

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.decorator import INPUT_PATH_ATTR
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

NETCDF_SUFFIXES = (".nc", ".nc4", ".netcdf")


def _keep(ds, variable, where):
    if not variable:
        return ds
    missing = [v for v in variable if v not in ds.data_vars]
    if missing:
        raise UsageError(
            f"--variable not in {where}: {', '.join(missing)} (have {', '.join(ds.data_vars)})"
        )
    return ds[variable]


def _export(ds, output, variable):
    """Write ``ds`` as one compressed NetCDF-4 file, attrs and history included."""
    ds = _keep(dequantify_dataset(ds), variable, "dataset")
    if not ds.data_vars:
        raise UsageError("dataset has no data variables to export")
    ds.attrs.pop(INPUT_PATH_ATTR, None)
    # NetCDF attributes hold text, numbers and arrays of them: booleans become
    # 0 / 1 and anything else (None, dicts, dates) its text.
    for attrs in [ds.attrs, *(ds.variables[name].attrs for name in ds.variables)]:
        for key, value in list(attrs.items()):
            array = np.asarray(value)
            if array.dtype == bool:
                attrs[key] = array.astype(np.int8) if array.ndim else int(array)
            elif array.dtype.kind not in "iufUS":
                attrs[key] = str(value)
    # Zarr chunking and compressors do not apply to NetCDF; let xarray pick.
    encoding = {}
    for name in ds.variables:
        ds[name].encoding = {}
        if name in ds.data_vars and np.issubdtype(ds[name].dtype, np.number):
            encoding[name] = {"zlib": True, "complevel": 4}
    output.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(output, format="NETCDF4", engine="netcdf4", encoding=encoding)
    size_mb = output.stat().st_size / 1e6
    print(f"Wrote: {output} ({size_mb:,.1f} MB)", file=sys.stderr)


def _read(paths, units):
    """Open each NetCDF file, join them on their coordinates, and fill in --units."""
    for path in paths:
        if not path.is_file():
            raise UsageError(f"NetCDF not found: {path}")
    parts = []
    for path in paths:
        try:
            parts.append(xr.open_dataset(path, engine="netcdf4"))
        except (OSError, ValueError) as exc:
            raise UsageError(f"cannot read {path} as NetCDF: {exc}") from None
    if len(parts) == 1:
        ds = parts[0]
    else:
        try:
            ds = xr.combine_by_coords(parts, combine_attrs="drop_conflicts")
        except ValueError as exc:
            raise UsageError(
                f"cannot join the {len(parts)} files on their coordinates: {exc}. "
                "Read them one at a time and join with concat or merge."
            ) from None
    # A coordinate value given twice (in one file, or in files that overlap) is
    # ambiguous, so refuse it; one out of order is only sorted.
    for dim, index in ds.indexes.items():
        repeated = index[index.duplicated()].unique()
        if len(repeated):
            labels = ", ".join(str(v) for v in repeated[:5])
            more = f" and {len(repeated) - 5} more" if len(repeated) > 5 else ""
            raise UsageError(
                f"{dim} {labels}{more} appear more than once"
                + (" (the files overlap)" if len(parts) > 1 else "")
                + ". Pass files that do not overlap, or read them one at a time."
            )
        if not (index.is_monotonic_increasing or index.is_monotonic_decreasing):
            ds = ds.sortby(dim)
    # Fixed-width text (classic NetCDF char arrays, e.g. station names) reads as
    # bytes; store it as text.
    for name in list(ds.variables):
        if ds[name].dtype.kind == "S":
            ds[name] = ds[name].str.decode("utf-8")
    if not ds.data_vars:
        raise UsageError(f"{', '.join(p.name for p in paths)} has no data variables")
    for spec in units or []:
        name, sep, unit = spec.partition("=")
        if not sep or not unit:
            raise UsageError(f"--units expects VAR=UNITS, e.g. tp=mm; got {spec!r}")
        if name not in ds.data_vars:
            raise UsageError(f"--units {name!r} not in NetCDF (have {', '.join(ds.data_vars)})")
        ds[name].attrs["units"] = unit
    no_units = [v for v in ds.data_vars if not ds[v].attrs.get("units")]
    if no_units:
        print(
            f"note: no units for {', '.join(no_units)}; pass --units NAME=UNITS "
            "(precipitation and temperature need units before later skills can use them)",
            file=sys.stderr,
        )
    ds = ensure_normalized_longitude(ds)
    ds.attrs["weather_skills_source"] = "netcdf:" + ",".join(p.name for p in paths)
    return ds


@weather_skill(name="netcdf-io", version=_SKILL_VERSION)
@weather_skill.argument(
    "-i", "--input", type=Dataset("any"), help="Zarr to export; -o names the .nc to write."
)
@weather_skill.argument(
    "--netcdf",
    type=Path,
    action="append",
    help="NetCDF file to read (repeatable, joined on coordinates); -o names the .zarr to write.",
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--units", action="append", help="Read: VAR=UNITS for a variable without a units attr."
)
def netcdf_io(ds, output, netcdf, variable, units, **kwargs):
    """Export a weather-skills Zarr to a NetCDF file, or read NetCDF files into a Zarr."""
    if (ds is None) == (netcdf is None):
        raise UsageError(
            "pass exactly one of -i/--input (Zarr to export) or --netcdf (NetCDF to read)"
        )
    if ds is not None:
        if output.suffix.lower() not in NETCDF_SUFFIXES:
            raise UsageError(f"export writes NetCDF; -o should end in .nc (got {output})")
        if units:
            raise UsageError("--units is for --netcdf; an exported Zarr keeps its own units")
        _export(ds, output, variable)
        return None
    if output.suffix.lower() in NETCDF_SUFFIXES:
        raise UsageError("reading NetCDF writes a Zarr; -o should end in .zarr")
    return _keep(_read(netcdf, units), variable, "NetCDF")


if __name__ == "__main__":
    netcdf_io()

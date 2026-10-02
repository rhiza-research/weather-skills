# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "scipy",
#   "xarray>=2026.4",
# ]
# ///
"""Sample a gridded dataset at point locations, writing a point_obs dataset."""

import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims, names_for
from weather_skills_core.standard_utils import ensure_normalized_longitude, grid_spacing
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_METHODS = ("nearest", "bilinear", "cell-mean")
_DEFAULT_NEIGHBORHOOD = 3
_CSV_ID = ("id", "station_id", "point_id")
_CSV_LAT = ("lat", "latitude")
_CSV_LON = ("lon", "longitude")


@dataclass
class _Points:
    """Point locations plus the coords to carry onto the output."""

    ids: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    dim: str = "station_id"
    lat_name: str = "latitude"
    lon_name: str = "longitude"
    id_attrs: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)  # name -> (values, attrs)


def _wrap_lon(lon):
    lon = np.asarray(lon, dtype=float)
    return np.where(lon > 180.0, ((lon + 180.0) % 360.0) - 180.0, lon)


def _points_from_dataset(pts):
    dim = next((n for n in names_for("point_id") if n in pts.dims), None)
    if dim is None:
        raise UsageError(f"--points has no station_id/point_id dim (dims: {list(pts.dims)})")

    def find(preferred):
        return next(
            (n for n in names_for(preferred) if n in pts.coords and pts[n].dims == (dim,)),
            None,
        )

    lat_name, lon_name = find("lat"), find("lon")
    if lat_name is None or lon_name is None:
        raise UsageError(
            f"--points needs 1-D latitude/longitude coords on {dim} (coords: {list(pts.coords)})"
        )
    extras = {
        name: (pts[name].values, dict(pts[name].attrs))
        for name in pts.coords
        if name not in (dim, lat_name, lon_name) and pts[name].dims == (dim,)
    }
    return _Points(
        ids=pts[dim].values,
        lat=np.asarray(pts[lat_name].values, dtype=float),
        lon=np.asarray(pts[lon_name].values, dtype=float),
        dim=dim,
        lat_name=lat_name,
        lon_name=lon_name,
        id_attrs=dict(pts[dim].attrs),
        extras=extras,
    )


def _parse_point(text, index):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) not in (2, 3):
        raise UsageError(f"--point expects LAT,LON[,ID]; got {text!r}")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise UsageError(f"--point expects numeric LAT,LON; got {text!r}") from None
    pid = parts[2] if len(parts) == 3 and parts[2] else f"P{index}"
    return pid, lat, lon


def _points_from_args(point):
    rows = [_parse_point(text, i) for i, text in enumerate(point)]
    return _Points(
        ids=np.array([r[0] for r in rows], dtype=object),
        lat=np.array([r[1] for r in rows], dtype=float),
        lon=np.array([r[2] for r in rows], dtype=float),
    )


def _points_from_csv(path):
    path = Path(path)
    if not path.exists():
        raise UsageError(f"--points-csv not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = {(h or "").strip().lower(): h for h in reader.fieldnames or []}

        def column(candidates, required):
            key = next((header[c] for c in candidates if c in header), None)
            if key is None and required:
                raise UsageError(
                    f"--points-csv needs a {candidates[0]!r} column "
                    f"(also accepted: {', '.join(candidates[1:])}); got {list(header)}"
                )
            return key

        id_key = column(_CSV_ID, required=False)
        lat_key, lon_key = column(_CSV_LAT, True), column(_CSV_LON, True)
        name_key = column(("name",), required=False)
        ids, lats, lons, names = [], [], [], []
        for i, row in enumerate(reader):
            try:
                lats.append(float(row[lat_key]))
                lons.append(float(row[lon_key]))
            except (TypeError, ValueError):
                raise UsageError(f"--points-csv row {i + 2}: non-numeric lat/lon") from None
            pid = (row.get(id_key) or "").strip() if id_key else ""
            ids.append(pid or f"P{i}")
            if name_key:
                names.append((row.get(name_key) or "").strip())
    if not ids:
        raise UsageError(f"--points-csv has no rows: {path}")
    extras = {}
    if name_key:
        extras["name"] = (np.array(names, dtype=object), {"long_name": "station name"})
    return _Points(
        ids=np.array(ids, dtype=object),
        lat=np.array(lats, dtype=float),
        lon=np.array(lons, dtype=float),
        extras=extras,
    )


def _check_points(points):
    if points.ids.size == 0:
        raise UsageError("no points to sample")
    if np.any(~np.isfinite(points.lat)) or np.any(~np.isfinite(points.lon)):
        raise UsageError("point latitude/longitude must be finite")
    if np.any(np.abs(points.lat) > 90.0):
        raise UsageError("point latitude must be within [-90, 90]")
    ids = [str(i) for i in points.ids]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise UsageError(f"duplicate point ids: {dupes[:10]}")
    points.lon = _wrap_lon(points.lon)


def _half_cell(axis):
    return 0.5 * grid_spacing(axis) if axis.size > 1 else np.inf


def _valid_cells(ds, lat_dim, lon_dim):
    """2-D mask of cells with any finite value in any selected variable."""
    valid = None
    for name in ds.data_vars:
        da = ds[name]
        if lat_dim not in da.dims or lon_dim not in da.dims:
            continue
        mask = da.notnull()
        other = [d for d in mask.dims if d not in (lat_dim, lon_dim)]
        if other:
            mask = mask.any(other)
        mask = mask.transpose(lat_dim, lon_dim)
        valid = mask if valid is None else (valid | mask)
    if valid is None:
        raise UsageError("no data variable has both grid lat/lon dims")
    return np.asarray(valid.values, dtype=bool)


def _anchor_cells(glat, glon, plat, plon, max_distance, valid):
    """Grid indices each point samples, distance (deg), and whether it is kept.

    Without ``max_distance`` the anchor is the cell containing the point; a
    point outside the grid is dropped. With ``max_distance`` the anchor is the
    nearest cell with data, and the point is dropped if none is that close.
    """
    n = plat.size
    ilat = np.abs(glat[None, :] - plat[:, None]).argmin(axis=1)
    ilon = np.abs(glon[None, :] - plon[:, None]).argmin(axis=1)
    dist = np.hypot(glat[ilat] - plat, glon[ilon] - plon)

    if max_distance is None:
        hlat, hlon = _half_cell(glat), _half_cell(glon)
        tol = 1e-9
        keep = (
            (plat >= glat.min() - hlat - tol)
            & (plat <= glat.max() + hlat + tol)
            & (plon >= glon.min() - hlon - tol)
            & (plon <= glon.max() + hlon + tol)
        )
        return ilat, ilon, dist, keep

    keep = np.zeros(n, dtype=bool)
    for p in range(n):
        rows = np.nonzero(np.abs(glat - plat[p]) <= max_distance)[0]
        cols = np.nonzero(np.abs(glon - plon[p]) <= max_distance)[0]
        if rows.size == 0 or cols.size == 0:
            continue
        ii, jj = np.nonzero(valid[np.ix_(rows, cols)])
        if ii.size == 0:
            continue
        d = np.hypot(glat[rows[ii]] - plat[p], glon[cols[jj]] - plon[p])
        k = int(np.argmin(d))
        if d[k] <= max_distance:
            ilat[p], ilon[p], dist[p], keep[p] = rows[ii[k]], cols[jj[k]], d[k], True
    return ilat, ilon, dist, keep


def _sample_cell_mean(ds, lat_dim, lon_dim, ilat, ilon, size, dim):
    """NaN-skipping mean of a size x size window centred on each anchor cell."""
    half = size // 2
    offsets = np.arange(-half, half + 1)
    di, dj = (a.ravel() for a in np.meshgrid(offsets, offsets, indexing="ij"))
    rows = ilat[:, None] + di[None, :]
    cols = ilon[:, None] + dj[None, :]
    inside = (rows >= 0) & (rows < ds.sizes[lat_dim]) & (cols >= 0) & (cols < ds.sizes[lon_dim])
    rows = np.clip(rows, 0, ds.sizes[lat_dim] - 1)
    cols = np.clip(cols, 0, ds.sizes[lon_dim] - 1)
    win = ds.isel(
        {
            lat_dim: xr.DataArray(rows, dims=(dim, "_window")),
            lon_dim: xr.DataArray(cols, dims=(dim, "_window")),
        }
    )
    win = win.where(xr.DataArray(inside, dims=(dim, "_window")))
    return win.mean("_window", skipna=True, keep_attrs=True)


def _append_cell_methods(attrs, text):
    existing = attrs.get("cell_methods")
    attrs["cell_methods"] = f"{existing} {text}" if existing else text


@weather_skill(
    name="point-value",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument(
    "--points",
    type=Dataset("point_id"),
    default=None,
    help="point_obs Zarr (TAHMO, GHCN-Daily, OpenAQ); reuses its ids, lat/lon, name, country.",
)
@weather_skill.argument(
    "--point",
    action="append",
    default=None,
    help=(
        "Ad-hoc location LAT,LON[,ID] (repeatable; ids default to P0, P1, ...). "
        "Use --point=-1.29,36.82 for a negative latitude."
    ),
)
@weather_skill.argument(
    "--points-csv",
    type=Path,
    default=None,
    help="CSV with columns id,lat,lon[,name] (latitude/longitude/station_id also accepted).",
)
@weather_skill.argument(
    "--method",
    choices=list(_METHODS),
    default="nearest",
    help=(
        "nearest: the grid cell containing the point (default); bilinear: linear "
        "interpolation at the point; cell-mean: mean of an N x N cell neighborhood."
    ),
)
@weather_skill.argument(
    "--neighborhood",
    type=int,
    default=None,
    help=f"Odd window width N for --method cell-mean (default {_DEFAULT_NEIGHBORHOOD}).",
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--max-distance",
    type=float,
    default=None,
    help=(
        "Degrees. Sample the nearest cell with data within this distance (points over "
        "masked cells or just off the grid); farther points become NaN."
    ),
)
def point_value(
    ds, points, point, points_csv, method, neighborhood, variable, max_distance, **kwargs
):
    """Sample a gridded dataset at point locations, writing a point_obs dataset."""
    given = [
        flag
        for flag, value in (("--points", points), ("--point", point), ("--points-csv", points_csv))
        if value
    ]
    if len(given) != 1:
        raise UsageError(
            "pass exactly one of --points, --point, --points-csv"
            + (f" (got {', '.join(given)})" if given else "")
        )
    if max_distance is not None and max_distance < 0:
        raise UsageError("--max-distance must be >= 0")
    if neighborhood is not None and method != "cell-mean":
        raise UsageError("--neighborhood only applies to --method cell-mean")
    size = _DEFAULT_NEIGHBORHOOD if neighborhood is None else neighborhood
    if size < 1 or size % 2 == 0:
        raise UsageError("--neighborhood must be a positive odd integer")

    if points is not None:
        pts = _points_from_dataset(points)
    elif point:
        pts = _points_from_args(point)
    else:
        pts = _points_from_csv(points_csv)
    _check_points(pts)

    lat_dim, lon_dim = detect_spatial_dims(ds)
    ds = dequantify_dataset(ds)
    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(f"variable(s) {missing} not in input: {list(ds.data_vars)}")
        ds = ds[list(dict.fromkeys(variable))]
    if pts.dim in ds.dims:
        raise UsageError(f"input already has a {pts.dim!r} dim; it must be a gridded dataset")
    ds = ensure_normalized_longitude(ds, lon_dim).sortby([lat_dim, lon_dim])
    glat = np.asarray(ds[lat_dim].values, dtype=float)
    glon = np.asarray(ds[lon_dim].values, dtype=float)

    valid = _valid_cells(ds, lat_dim, lon_dim) if max_distance is not None else None
    ilat, ilon, dist, keep = _anchor_cells(glat, glon, pts.lat, pts.lon, max_distance, valid)
    dim = pts.dim

    if method == "nearest":
        out = ds.isel(
            {lat_dim: xr.DataArray(ilat, dims=dim), lon_dim: xr.DataArray(ilon, dims=dim)}
        )
    elif method == "cell-mean":
        out = _sample_cell_mean(ds, lat_dim, lon_dim, ilat, ilon, size, dim)
    else:
        out = ds.interp(
            {
                lat_dim: xr.DataArray(pts.lat, dims=dim),
                lon_dim: xr.DataArray(pts.lon, dims=dim),
            },
            method="linear",
        )
    out = out.drop_vars([lat_dim, lon_dim], errors="ignore")
    out = out.where(xr.DataArray(keep, dims=dim))

    for name in out.data_vars:
        attrs = dict(ds[name].attrs)
        if method == "cell-mean":
            _append_cell_methods(attrs, f"area: mean ({size}x{size} grid-cell neighborhood)")
        out[name].attrs = attrs

    grid_lat = np.where(keep, glat[ilat], np.nan)
    grid_lon = np.where(keep, glon[ilon], np.nan)
    coords = {
        dim: (dim, pts.ids, {"cf_role": "timeseries_id", **pts.id_attrs}),
        pts.lat_name: (
            dim,
            pts.lat,
            {"standard_name": "latitude", "units": "degrees_north", "long_name": "point latitude"},
        ),
        pts.lon_name: (
            dim,
            pts.lon,
            {
                "standard_name": "longitude",
                "units": "degrees_east",
                "long_name": "point longitude",
            },
        ),
        "grid_latitude": (
            dim,
            grid_lat,
            {"units": "degrees_north", "long_name": "latitude of the sampled grid cell"},
        ),
        "grid_longitude": (
            dim,
            grid_lon,
            {"units": "degrees_east", "long_name": "longitude of the sampled grid cell"},
        ),
        "grid_distance": (
            dim,
            np.where(keep, dist, np.nan),
            {"units": "degree", "long_name": "distance from point to sampled grid-cell center"},
        ),
    }
    for name, (values, attrs) in pts.extras.items():
        coords[name] = (dim, values, attrs)
    out = out.assign_coords(coords)
    out = out.transpose(dim, ...)

    dropped = [str(i) for i, k in zip(pts.ids, keep, strict=True) if not k]
    if dropped:
        why = (
            f"no cell with data within --max-distance {max_distance}°"
            if max_distance is not None
            else "outside the grid (pass --max-distance to snap to the nearest cell)"
        )
        shown = ", ".join(dropped[:10]) + (" ..." if len(dropped) > 10 else "")
        print(f"note: {len(dropped)} point(s) set to NaN, {why}: {shown}", file=sys.stderr)
    print(
        f"sampled {len(pts.ids) - len(dropped)}/{len(pts.ids)} point(s) with --method {method}",
        file=sys.stderr,
    )

    out.attrs = dict(ds.attrs)
    out.attrs.update(
        featureType="timeSeries",
        Conventions="CF-1.13",
        point_value_method=method if method != "cell-mean" else f"cell-mean {size}x{size}",
    )
    return out


if __name__ == "__main__":
    point_value()

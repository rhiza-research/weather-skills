# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "scipy",
# ]
# ///
"""Sample a gridded Zarr at point locations and write a station Zarr."""

import csv
import sys
from pathlib import Path

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims, names_for
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"


def _load_points(points, point, points_csv):
    """Points as a coords-only Dataset: (dim, lat name, lon name, coords)."""
    if points is not None:
        dim = next((n for n in names_for("point_id") if n in points.dims), None)
        lat = next((n for n in names_for("lat") if n in points.coords), None)
        lon = next((n for n in names_for("lon") if n in points.coords), None)
        if None in (dim, lat, lon):
            raise UsageError("--points needs a station_id/point_id dim with latitude/longitude")
        coords = {c: points[c] for c in points.coords if points[c].dims == (dim,)}
        return dim, lat, lon, xr.Dataset(coords=coords)
    if point:
        rows = [p.split(",") for p in point]
        if any(len(r) not in (2, 3) for r in rows):
            raise UsageError("--point expects LAT,LON[,ID]")
        rows = [{"lat": r[0], "lon": r[1], "id": r[2] if len(r) == 3 else ""} for r in rows]
    else:
        with Path(points_csv).open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if not rows or not {"lat", "lon"} <= rows[0].keys():
            raise UsageError("--points-csv needs id,lat,lon[,name] columns and at least one row")
    try:
        coords = {
            "station_id": [r.get("id") or f"P{i}" for i, r in enumerate(rows)],
            "latitude": ("station_id", [float(r["lat"]) for r in rows]),
            "longitude": ("station_id", [float(r["lon"]) for r in rows]),
        }
    except ValueError:
        raise UsageError("point lat/lon must be numeric") from None
    if "name" in rows[0]:
        coords["name"] = ("station_id", [r["name"] for r in rows])
    return "station_id", "latitude", "longitude", xr.Dataset(coords=coords)


@weather_skill(name="point-value", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument(
    "--points", type=Dataset("point_id"), help="point_obs Zarr whose stations to sample."
)
@weather_skill.argument(
    "--point",
    action="append",
    help="LAT,LON[,ID] (repeatable), e.g. --point -1.29,36.82,nairobi.",
)
@weather_skill.argument("--points-csv", type=Path, help="CSV with columns id,lat,lon[,name].")
@weather_skill.argument("--method", choices=["nearest", "bilinear", "cell-mean"], default="nearest")
@weather_skill.argument(
    "--neighborhood", type=int, default=3, help="Odd window width for --method cell-mean."
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--max-distance",
    type=float,
    help="Degrees: snap to the nearest cell with data within this distance, else NaN.",
)
def point_value(
    ds, points, point, points_csv, method, neighborhood, variable, max_distance, **kwargs
):
    """Sample a gridded Zarr at point locations and write a station Zarr."""
    if sum(x is not None for x in (points, point, points_csv)) != 1:
        raise UsageError("pass exactly one of --points, --point, --points-csv")
    if neighborhood < 1 or neighborhood % 2 == 0:
        raise UsageError("--neighborhood must be a positive odd integer")
    dim, lat_name, lon_name, pts = _load_points(points, point, points_csv)
    if pts.indexes[dim].has_duplicates:
        raise UsageError("point ids must be unique")
    pts = ensure_normalized_longitude(pts, lon_name)
    plat, plon = pts[lat_name].values.astype(float), pts[lon_name].values.astype(float)

    lat_dim, lon_dim = detect_spatial_dims(ds)
    ds = dequantify_dataset(ensure_normalized_longitude(ds, lon_dim))
    ds = ds[variable] if variable else ds
    # sortby copies the whole grid; skip it when the axes already ascend.
    if not all(ds.indexes[d].is_monotonic_increasing for d in (lat_dim, lon_dim)):
        ds = ds.sortby([lat_dim, lon_dim])
    glat, glon = ds[lat_dim].values.astype(float), ds[lon_dim].values.astype(float)

    # Anchor cell: the one containing the point (kept if within half a cell of it),
    # or with --max-distance the nearest cell holding any finite value.
    ilat = np.abs(glat[None] - plat[:, None]).argmin(1)
    ilon = np.abs(glon[None] - plon[:, None]).argmin(1)
    if max_distance is None:
        # 1.001: a point on a cell edge stays in, despite float32 coords.
        half_lat, half_lon = (
            np.median(np.diff(g)) / 2 * 1.001 if g.size > 1 else np.inf for g in (glat, glon)
        )
        keep = (np.abs(glat[ilat] - plat) <= half_lat) & (np.abs(glon[ilon] - plon) <= half_lon)
    else:
        keep = np.zeros(plat.size, bool)
        for p in range(plat.size):
            rows = np.flatnonzero(np.abs(glat - plat[p]) <= max_distance)
            cols = np.flatnonzero(np.abs(glon - plon[p]) <= max_distance)
            # Cells with any finite value, checked only in this point's window.
            valid = ds.isel({lat_dim: rows, lon_dim: cols}).to_array().notnull()
            valid = valid.any([d for d in valid.dims if d not in (lat_dim, lon_dim)])
            ii, jj = np.nonzero(valid.transpose(lat_dim, lon_dim).values)
            dist = np.hypot(glat[rows[ii]] - plat[p], glon[cols[jj]] - plon[p])
            if dist.size and dist.min() <= max_distance:
                k = dist.argmin()
                ilat[p], ilon[p], keep[p] = rows[ii[k]], cols[jj[k]], True

    anchor = {lat_dim: xr.DataArray(ilat, dims=dim), lon_dim: xr.DataArray(ilon, dims=dim)}
    if method == "bilinear":
        at = {lat_dim: xr.DataArray(plat, dims=dim), lon_dim: xr.DataArray(plon, dims=dim)}
        out = ds.interp(at, method="linear")
    elif method == "cell-mean":
        # Gather only the N x N window around each anchor cell (not a rolling
        # mean over the whole grid); cells past the grid edge are left out.
        offsets = np.arange(neighborhood) - neighborhood // 2
        rows, cols = ilat[:, None] + offsets, ilon[:, None] + offsets
        on_grid = ((rows >= 0) & (rows < glat.size))[:, :, None] & (
            (cols >= 0) & (cols < glon.size)
        )[:, None, :]
        window = ds.isel(
            {
                lat_dim: xr.DataArray(rows.clip(0, glat.size - 1), dims=(dim, "_wlat")),
                lon_dim: xr.DataArray(cols.clip(0, glon.size - 1), dims=(dim, "_wlon")),
            }
        )
        on_grid = xr.DataArray(on_grid, dims=(dim, "_wlat", "_wlon"))
        out = window.where(on_grid).mean(["_wlat", "_wlon"], skipna=True, keep_attrs=True)
        for v in out.data_vars:
            cm = f"area: mean ({neighborhood}x{neighborhood} grid-cell neighborhood)"
            out[v].attrs["cell_methods"] = " ".join(
                filter(None, [ds[v].attrs.get("cell_methods"), cm])
            )
    else:
        out = ds.isel(anchor)

    out = out.drop_vars([lat_dim, lon_dim], errors="ignore").where(xr.DataArray(keep, dims=dim))
    # Where each value was sampled. Plain "degrees", not degrees_north/east, so
    # CF latitude/longitude detection still finds only the point's own lat/lon.
    grid_lat = {"long_name": "latitude of the sampled grid cell centre", "units": "degrees"}
    grid_lon = {"long_name": "longitude of the sampled grid cell centre", "units": "degrees"}
    out = out.assign_coords(
        {
            **pts.coords,
            "grid_latitude": (dim, np.where(keep, glat[ilat], np.nan), grid_lat),
            "grid_longitude": (dim, np.where(keep, glon[ilon], np.nan), grid_lon),
        }
    ).transpose(dim, ...)
    out[dim].attrs["cf_role"] = "timeseries_id"
    out.attrs.update(featureType="timeSeries", Conventions="CF-1.13")

    if not keep.all():
        dropped = ", ".join(map(str, pts[dim].values[~keep][:10]))
        print(
            f"note: {(~keep).sum()} point(s) set to NaN (no cell nearby): {dropped}",
            file=sys.stderr,
        )
    return out


if __name__ == "__main__":
    point_value()

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
from pathlib import Path

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims, names_for
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


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
    help="LAT,LON[,ID] (repeatable). Use --point=-1.29,36.82 for a negative latitude.",
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
    """Sample a gridded dataset at point locations, writing a point_obs dataset."""
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
    ds = dequantify_dataset(ensure_normalized_longitude(ds, lon_dim)).sortby([lat_dim, lon_dim])
    ds = ds[variable] if variable else ds
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
        valid = ds.to_array().notnull()
        valid = valid.any([d for d in valid.dims if d not in (lat_dim, lon_dim)])
        valid = valid.transpose(lat_dim, lon_dim).values
        keep = np.zeros(plat.size, bool)
        for p in range(plat.size):
            rows = np.flatnonzero(np.abs(glat - plat[p]) <= max_distance)
            cols = np.flatnonzero(np.abs(glon - plon[p]) <= max_distance)
            ii, jj = np.nonzero(valid[np.ix_(rows, cols)])
            dist = np.hypot(glat[rows[ii]] - plat[p], glon[cols[jj]] - plon[p])
            if dist.size and dist.min() <= max_distance:
                k = dist.argmin()
                ilat[p], ilon[p], keep[p] = rows[ii[k]], cols[jj[k]], True

    anchor = {lat_dim: xr.DataArray(ilat, dims=dim), lon_dim: xr.DataArray(ilon, dims=dim)}
    if method == "bilinear":
        at = {lat_dim: xr.DataArray(plat, dims=dim), lon_dim: xr.DataArray(plon, dims=dim)}
        out = ds.interp(at, method="linear")
    elif method == "cell-mean":
        window = {lat_dim: neighborhood, lon_dim: neighborhood}
        out = ds.rolling(window, center=True, min_periods=1).mean(keep_attrs=True).isel(anchor)
        for v in out.data_vars:
            cm = f"area: mean ({neighborhood}x{neighborhood} grid-cell neighborhood)"
            out[v].attrs["cell_methods"] = " ".join(
                filter(None, [ds[v].attrs.get("cell_methods"), cm])
            )
    else:
        out = ds.isel(anchor)

    out = out.drop_vars([lat_dim, lon_dim]).where(xr.DataArray(keep, dims=dim))
    out = out.assign_coords(
        {
            **pts.coords,
            "grid_latitude": (dim, np.where(keep, glat[ilat], np.nan), {"units": "degrees_north"}),
            "grid_longitude": (dim, np.where(keep, glon[ilon], np.nan), {"units": "degrees_east"}),
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

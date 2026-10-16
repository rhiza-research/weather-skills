# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Find the local highs or lows of a gridded field, writing a point_obs dataset."""

import sys

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# A centre's --radius window must span at least this fraction of the map's range.
MIN_CONTRAST = 0.05


def _window(z, kx, ky, fn, fill):
    """``fn`` (np.maximum, np.minimum, np.add) over a (2ky+1) x (2kx+1) window at every cell."""
    ny, nx = z.shape
    pad = np.pad(z, ((ky, ky), (kx, kx)), constant_values=fill)
    rows = pad[:, :nx].copy()
    for i in range(1, 2 * kx + 1):
        fn(rows, pad[:, i : i + nx], out=rows)
    out = rows[:ny].copy()
    for j in range(1, 2 * ky + 1):
        fn(out, rows[j : j + ny], out=out)
    return out


@weather_skill(name="local-extrema", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument("--kind", choices=["high", "low"], required=True)
@weather_skill.argument(
    "--radius", type=float, default=10.0, help="Degrees: at most one centre within this distance."
)
@weather_skill.argument("--variable", "-v", help="Field to search (default: the only variable).")
def local_extrema(ds, kind, radius, variable, **kwargs):
    """Find the local highs or lows of a gridded field, writing a point_obs dataset."""
    if radius <= 0:
        raise UsageError("--radius must be a positive number of degrees")
    if variable is None:
        if len(ds.data_vars) != 1:
            raise UsageError(f"pass --variable; the input has {sorted(ds.data_vars)}")
        variable = next(iter(ds.data_vars))
    if variable not in ds.data_vars:
        raise UsageError(f"--variable {variable!r} not in the input; have {sorted(ds.data_vars)}")
    lat_dim, lon_dim = detect_spatial_dims(ds)
    ds = dequantify_dataset(ensure_normalized_longitude(ds, lon_dim))
    da = ds[variable]
    other = [d for d in da.dims if d not in (lat_dim, lon_dim)]
    da = da.transpose(*other, lat_dim, lon_dim)
    lat, lon = da[lat_dim].values.astype(float), da[lon_dim].values.astype(float)
    ny, nx = lat.size, lon.size
    kx = max(1, round(radius / abs(lon[1] - lon[0]))) if nx > 1 else 1
    ky = max(1, round(radius / abs(lat[1] - lat[0]))) if ny > 1 else 1
    # Smoothing over a quarter radius keeps small bumps (MSLP over mountains) out.
    sx, sy = max(1, kx // 4), max(1, ky // 4)
    sign = 1.0 if kind == "high" else -1.0

    maps = np.asarray(da.values, dtype=float).reshape(-1, ny, nx)
    found = np.full(maps.shape, np.nan)
    for k, z in enumerate(maps):
        # Search sign * z for maxima, so a low is a high of -z.
        z = sign * z
        finite = np.isfinite(z)
        if not finite.any():
            continue
        total = _window(np.where(finite, z, 0.0), sx, sy, np.add, 0.0)
        count = _window(finite.astype(float), sx, sy, np.add, 0.0)
        smooth = np.where(finite, total / np.maximum(count, 1), -np.inf)
        hi = _window(smooth, kx, ky, np.maximum, -np.inf)
        lo = _window(np.where(finite, smooth, np.inf), kx, ky, np.minimum, np.inf)
        span = smooth[finite].max() - smooth[finite].min()
        hit = finite & (smooth == hi) & (hi - lo >= max(MIN_CONTRAST * span, np.finfo(float).tiny))
        # A field still rising off the map edge has no centre there.
        edge = np.ones_like(hit)
        edge[sy:-sy, sx:-sx] = False
        hit &= ~edge
        peak = _window(np.where(finite, z, -np.inf), sx, sy, np.maximum, -np.inf)
        iy, ix = np.nonzero(hit)
        kept = []
        for i in np.argsort(-smooth[iy, ix], kind="stable"):
            if all(abs(iy[i] - a) > ky or abs(ix[i] - b) > kx for a, b in kept):
                kept.append((iy[i], ix[i]))
                found[k, iy[i], ix[i]] = sign * peak[iy[i], ix[i]]

    # One point per grid cell that is a centre in any map, strongest first.
    iy, ix = np.nonzero(np.isfinite(found).any(axis=0))
    order = np.argsort(-np.nanmax(sign * found[:, iy, ix], axis=0), kind="stable")
    iy, ix = iy[order], ix[order]
    letter = "H" if kind == "high" else "L"
    values = found[:, iy, ix].reshape(*(da.sizes[d] for d in other), iy.size)
    coords = {c: da[c] for c in da.coords if not {lat_dim, lon_dim} & set(da[c].dims)}
    out = xr.DataArray(
        values,
        dims=(*other, "station_id"),
        coords={
            **coords,
            "station_id": [f"{letter}{n + 1}" for n in range(iy.size)],
            "latitude": ("station_id", lat[iy]),
            "longitude": ("station_id", lon[ix]),
        },
        attrs={**da.attrs, "extremum_kind": kind, "extremum_radius_deg": radius},
    ).transpose("station_id", ...)
    out = out.to_dataset(name=variable)
    out["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    out["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    out["station_id"].attrs.update(cf_role="timeseries_id", long_name=f"{kind} centre id")
    out.attrs.update(featureType="timeSeries", Conventions="CF-1.13")
    if iy.size == 0:
        print(f"note: no {kind} centres found; try a smaller --radius", file=sys.stderr)
    return out


if __name__ == "__main__":
    local_extrema()

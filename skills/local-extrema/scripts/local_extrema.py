# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Find the local highs or lows of a gridded field and write them as station-style points."""
# Per map (each combination of the non-spatial dims), a centre is a cell whose
# box-smoothed value is the extreme within --radius, with enough contrast in that
# window, and whose smoothing box lies wholly on the map and on data. Each centre
# becomes a point (station_id) holding the raw extreme under its smoothing box;
# points are NaN in maps where they are not a centre.

import sys

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import ensure_normalized_longitude
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

# A centre's --radius window must span at least this fraction of the map's range.
MIN_CONTRAST = 0.05


def _window(z, kx, ky, fn, fill, wrap=False):
    """Reduce ``z`` over a (2ky+1) x (2kx+1) window centred on every cell.

    Separable: reduce along longitude, then latitude.

    Args:
        z: 2-D (lat, lon) array.
        kx, ky: window half-widths in cells along lon and lat.
        fn: ufunc applied pairwise: np.maximum, np.minimum, or np.add (box sum).
        fill: value for cells off the map (e.g. -inf for a max, 0 for a sum).
        wrap: join the last longitude to the first, for a grid that circles the globe.

    Returns:
        Array shaped like ``z``.
    """
    ny, nx = z.shape
    pad = np.pad(z, ((ky, ky), (0, 0)), constant_values=fill)
    if wrap:
        pad = np.pad(pad, ((0, 0), (kx, kx)), mode="wrap")
    else:
        pad = np.pad(pad, ((0, 0), (kx, kx)), constant_values=fill)
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
    """Find the local highs or lows of a gridded field and write them as station-style points."""
    # Returns a CF ``timeSeries`` dataset: ``variable`` on ``(station_id, *other dims)``,
    # ids ``H1``/``L1``... strongest first, lat/lon coords at each point's first cell.
    if not np.isfinite(radius) or radius <= 0:
        raise UsageError("--radius must be a positive number of degrees")
    if variable is None:
        if len(ds.data_vars) != 1:
            raise UsageError(f"pass --variable; the input has {sorted(ds.data_vars)}")
        variable = next(iter(ds.data_vars))
    if variable not in ds.data_vars:
        raise UsageError(f"--variable {variable!r} not in the input; have {sorted(ds.data_vars)}")
    lat_dim, lon_dim = detect_spatial_dims(ds)
    ds = dequantify_dataset(ensure_normalized_longitude(ds, lon_dim))
    da = ds[variable].sortby(lon_dim)
    lon = da[lon_dim].values.astype(float)
    # Spacing between neighbouring longitudes, including last -> first across 360.
    gaps = np.diff(lon, append=lon[0] + 360)
    dlon = np.median(gaps)
    # A grid with no gap wider than a cell circles the globe, so windows wrap around it.
    wrap = lon.size > 1 and gaps.max() < 1.5 * dlon
    if not wrap:
        # Start after the widest gap, so a bbox across the dateline is one contiguous map.
        da = da.roll({lon_dim: -((np.argmax(gaps) + 1) % lon.size)}, roll_coords=True)
    other = [d for d in da.dims if d not in (lat_dim, lon_dim)]
    da = da.transpose(*other, lat_dim, lon_dim)
    lat, lon = da[lat_dim].values.astype(float), da[lon_dim].values.astype(float)
    ny, nx = lat.size, lon.size
    # Half-widths in cells: k* for the --radius search window, s* for smoothing.
    kx = max(1, round(radius / dlon))
    ky = max(1, round(radius / np.median(np.abs(np.diff(lat))))) if ny > 1 else 1
    # Smoothing over a quarter radius keeps single noisy cells and small bumps out.
    sx, sy = max(1, kx // 4), max(1, ky // 4)
    sign = 1.0 if kind == "high" else -1.0

    maps = da.values.reshape(-1, ny, nx)
    anchors = []  # each point's grid cell, the first place it was found
    cells = {}  # anchor cell -> point index
    found = {}  # (map index, point index) -> central value
    for k, z in enumerate(maps):
        # Search sign * z for maxima, so a low is a high of -z.
        z = sign * z.astype(float)
        finite = np.isfinite(z)
        if not finite.any():
            continue
        # Box mean over the cells that have data.
        total = _window(np.where(finite, z, 0.0), sx, sy, np.add, 0.0, wrap)
        count = _window(finite.astype(float), sx, sy, np.add, 0.0, wrap)
        smooth = np.where(finite, total / np.maximum(count, 1), -np.inf)
        # Largest and smallest smoothed value within --radius of each cell.
        hi = _window(smooth, kx, ky, np.maximum, -np.inf, wrap)
        lo = _window(np.where(finite, smooth, np.inf), kx, ky, np.minimum, np.inf, wrap)
        span = smooth[finite].max() - smooth[finite].min()
        # The smoothing box must lie wholly on the map and on data: a field still
        # rising off the map edge, or toward masked land, has no centre there.
        full = count == (2 * sx + 1) * (2 * sy + 1)
        # A centre is the window's maximum and the window is not flat.
        hit = full & (smooth == hi) & (hi - lo >= max(MIN_CONTRAST * span, np.finfo(float).tiny))
        # The value reported: the raw extreme under the smoothing box.
        peak = _window(np.where(finite, z, -np.inf), sx, sy, np.maximum, -np.inf, wrap)
        iy, ix = np.nonzero(hit)
        # Strongest first; drop a tie within --radius of a centre already kept.
        kept = []
        for i in np.argsort(-smooth[iy, ix], kind="stable"):
            y, x = int(iy[i]), int(ix[i])
            near = False
            for a, b in kept:
                dx = abs(x - b)
                near |= abs(y - a) <= ky and min(dx, nx - dx if wrap else dx) <= kx
            if near:
                continue
            kept.append((y, x))
            # A centre one cell from a point found in an earlier map is that point,
            # so a system that jitters by a cell keeps its id.
            ring = [
                (y + a, (x + b) % nx if wrap else x + b) for a in (-1, 0, 1) for b in (-1, 0, 1)
            ]
            p = next((cells[c] for c in ring if c in cells and (k, cells[c]) not in found), None)
            if p is None:
                p = len(anchors)
                anchors.append((y, x))
                cells.setdefault((y, x), p)
            found[(k, p)] = sign * peak[y, x]

    # Dense (map, point) values, points ordered by their strongest value.
    values = np.full((len(maps), len(anchors)), np.nan)
    for (k, p), v in found.items():
        values[k, p] = v
    order = np.argsort(-np.nanmax(sign * values, axis=0), kind="stable")
    iy = np.array([anchors[p][0] for p in order], dtype=int)
    ix = np.array([anchors[p][1] for p in order], dtype=int)
    letter = "H" if kind == "high" else "L"
    values = values[:, order].reshape(*(da.sizes[d] for d in other), iy.size)
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

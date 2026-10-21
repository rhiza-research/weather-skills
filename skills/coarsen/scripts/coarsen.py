# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "xarray-regrid",
# ]
# ///
"""Put a gridded Zarr onto a coarser or equally spaced lat/lon grid."""

import math
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import ensure_normalized_longitude, grid_spacing
from weather_skills_core.units import dequantify_dataset

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"


def _target_axis(coord_vals, resolution, offset):
    import numpy as np

    vmin, vmax = float(np.min(coord_vals)), float(np.max(coord_vals))
    eps = 1e-9 * max(1.0, abs(vmin), abs(vmax)) / resolution
    k_min = math.ceil((vmin - offset) / resolution - eps)
    k_max = math.floor((vmax - offset) / resolution + eps)
    target = offset + np.arange(k_min, k_max + 1) * resolution
    if coord_vals[0] > coord_vals[-1]:
        target = target[::-1]
    return target


def _clip_reference_axis(input_vals, ref_vals):
    """Keep reference coordinate values that fall within the input span."""
    import numpy as np

    ref = np.asarray(ref_vals, dtype=float)
    if ref.size == 0:
        raise UsageError("--reference-grid has an empty lat/lon axis")
    vmin, vmax = float(np.min(input_vals)), float(np.max(input_vals))
    if ref.size > 1:
        spacing = float(np.median(np.abs(np.diff(np.sort(np.unique(ref))))))
    else:
        spacing = 0.0
    # Half a cell past each edge, plus slack for float32 coords (a reference
    # point exactly half a cell out would otherwise fall just outside).
    pad = 0.5 * spacing + 1e-4
    kept = ref[(ref >= vmin - pad) & (ref <= vmax + pad)]
    if kept.size == 0:
        raise UsageError(
            "--reference-grid lat/lon does not overlap the input spatial extent; "
            "nothing to regrid onto"
        )
    # Match the input's monotonic direction so regrid gets a consistent axis.
    if input_vals[0] > input_vals[-1] and kept.size > 1 and kept[0] < kept[-1]:
        kept = kept[::-1].copy()
    elif input_vals[0] < input_vals[-1] and kept.size > 1 and kept[0] > kept[-1]:
        kept = kept[::-1].copy()
    return kept


@weather_skill(
    name="coarsen",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--reference-grid",
    help=(
        "Zarr whose lat/lon become the target (exact coordinate values). "
        "Prefer this when matching another dataset for difference/verify."
    ),
)
@weather_skill.argument(
    "--target-resolution",
    type=float,
    help="Target spacing in degrees (with --offset). Prefer --reference-grid to match a Zarr.",
)
@weather_skill.argument(
    "--offset",
    type=float,
    help="Grid offset in degrees; points at offset + k*resolution (with --target-resolution).",
)
@weather_skill.argument(
    "--method",
    default="linear",
    choices=["linear", "conservative"],
    help=(
        "linear: interpolate at each target point (realigns, same-resolution shifts). "
        "conservative: area-weighted mean of the source cells inside each target "
        "cell — use it to coarsen fine obs (0.05°) onto a coarse forecast grid."
    ),
)
@weather_skill.argument(
    "--min-fraction",
    type=float,
    default=1.0,
    help=(
        "With --method conservative: least fraction of a target cell's area that "
        "must have finite source data (default 1.0: any missing source cell makes "
        "the target cell NaN). Lower it to average only the finite part."
    ),
)
def coarsen(
    ds, variable, reference_grid, target_resolution, offset, method, min_fraction, **kwargs
):
    """Put a gridded Zarr onto a coarser or equally spaced lat/lon grid."""
    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401

    has_ref = reference_grid is not None
    has_pair = target_resolution is not None and offset is not None
    has_partial = (target_resolution is not None) ^ (offset is not None)
    if has_ref and (target_resolution is not None or offset is not None):
        raise UsageError(
            "pass --reference-grid alone, or --target-resolution with --offset, not both"
        )
    if has_partial:
        raise UsageError("--target-resolution and --offset must be passed together")
    if not has_ref and not has_pair:
        raise UsageError(
            "pass --reference-grid PATH (preferred when matching another Zarr), "
            "or both --target-resolution and --offset"
        )
    if target_resolution is not None and target_resolution <= 0:
        raise UsageError("--target-resolution must be > 0")
    if not 0.0 < min_fraction <= 1.0:
        raise UsageError("--min-fraction must be in (0, 1]")
    if method != "conservative" and min_fraction != 1.0:
        raise UsageError("--min-fraction applies only to --method conservative")

    lat_dim, lon_dim = detect_spatial_dims(ds)
    if variable:
        ds = ds[[variable]]
    # xarray-regrid can't interpolate pint quantities wrapping dask arrays, and
    # fails on coords chunked unlike the data (aggregation_coverage on time);
    # coords are small, so load them.
    ds = dequantify_dataset(ensure_normalized_longitude(ds, lon_dim))
    ds = ds.assign_coords({c: ds[c].compute() for c in ds.coords if ds[c].chunks})
    in_lat, in_lon = grid_spacing(ds[lat_dim].values), grid_spacing(ds[lon_dim].values)

    if has_ref:
        ref = xr.open_zarr(reference_grid, consolidated=False)
        ref_lat_dim, ref_lon_dim = detect_spatial_dims(ref)
        ref = ensure_normalized_longitude(ref, ref_lon_dim)
        ref_lat_sp = grid_spacing(ref[ref_lat_dim].values)
        ref_lon_sp = grid_spacing(ref[ref_lon_dim].values)
        # 1% rel_tol: float noise / half-cell shift is not a resolution change.
        if (ref_lat_sp < in_lat and not math.isclose(ref_lat_sp, in_lat, rel_tol=1e-2)) or (
            ref_lon_sp < in_lon and not math.isclose(ref_lon_sp, in_lon, rel_tol=1e-2)
        ):
            raise UsageError("--reference-grid is finer than input; use downscale --reference-grid")
        new_lat = _clip_reference_axis(ds[lat_dim].values, ref[ref_lat_dim].values)
        new_lon = _clip_reference_axis(ds[lon_dim].values, ref[ref_lon_dim].values)
        lat_attrs = dict(ref[ref_lat_dim].attrs) or dict(ds[lat_dim].attrs)
        lon_attrs = dict(ref[ref_lon_dim].attrs) or dict(ds[lon_dim].attrs)
    else:
        # Reject finer targets (that's downscale)
        for spacing in (in_lat, in_lon):
            if target_resolution < spacing and not math.isclose(
                target_resolution, spacing, rel_tol=1e-2
            ):
                raise UsageError(
                    f"--target-resolution {target_resolution}° finer than input; use downscale"
                )
        new_lat = _target_axis(ds[lat_dim].values, target_resolution, offset)
        new_lon = _target_axis(ds[lon_dim].values, target_resolution, offset)
        lat_attrs = dict(ds[lat_dim].attrs)
        lon_attrs = dict(ds[lon_dim].attrs)

    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, lat_attrs),
            lon_dim: (lon_dim, new_lon, lon_attrs),
        }
    )
    # Regrid only the variables on the grid; others (no lat/lon) pass through,
    # since xarray-regrid's conservative method fails on them.
    gridded = [v for v in ds.data_vars if {lat_dim, lon_dim} <= set(ds[v].dims)]
    if not gridded:
        raise UsageError("no data variable has both lat and lon dims; nothing to coarsen")
    other = ds.drop_vars(gridded)
    if method == "linear":
        return ds[gridded].regrid.linear(target).assign(other.data_vars)

    # Area-weighted (cos-lat) mean over each target cell. nan_threshold is the
    # largest NaN area fraction kept, so 1 - min_fraction. Missing source
    # cells are never filled; at the default they make the target cell NaN.
    out = ds[gridded].regrid.conservative(
        target, latitude_coord=lat_dim, nan_threshold=1.0 - min_fraction
    )
    # xarray-regrid also averages the covered part of a target cell that hangs
    # past the input's edge (once over half is covered). Hold that uncovered
    # area to --min-fraction too: per axis, the share of each target cell
    # inside the input's cell edges.
    inside = []
    for dim, new in ((lat_dim, new_lat), (lon_dim, new_lon)):
        src = np.sort(np.asarray(ds[dim].values, dtype=float))
        new = np.asarray(new, dtype=float)
        src_half = (src[1] - src[0]) / 2 if src.size > 1 else 0.0
        new_half = grid_spacing(new) / 2 if new.size > 1 else src_half
        lo = np.maximum(new - new_half, src[0] - src_half)
        hi = np.minimum(new + new_half, src[-1] + src_half)
        inside.append(np.clip(hi - lo, 0.0, None) / (2 * new_half))
    fraction = np.outer(inside[0], inside[1])
    covered = xr.DataArray(fraction >= min_fraction - 1e-3, dims=(lat_dim, lon_dim))
    n_edge = int((~covered.values).sum())
    if n_edge:
        half = grid_spacing(np.asarray(new_lat, dtype=float)) / 2 if len(new_lat) > 1 else 0.0
        print(
            f"Note: {n_edge} of {fraction.size} target cells hang past the input's edge "
            f"(under {min_fraction:.0%} covered) and are NaN. Fetch the input half a target "
            f"cell (~{half:.3g}°) wider on each side, or lower --min-fraction.",
            file=sys.stderr,
        )
    return out.assign({v: out[v].where(covered) for v in gridded}).assign(other.data_vars)


if __name__ == "__main__":
    coarsen()

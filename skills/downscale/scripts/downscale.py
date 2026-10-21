# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "xarray-regrid",
# ]
# ///
"""Put a gridded Zarr onto a finer or equally spaced lat/lon grid, optionally with quantile mapping."""

import math
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import ensure_normalized_longitude, grid_spacing
from weather_skills_core.units import units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"


def _spacing(ds, dim):
    return grid_spacing(ds[dim].values)


def _target_coord(coord, new_spacing):
    import numpy as np

    vmin, vmax = float(np.min(coord)), float(np.max(coord))
    target = vmin + new_spacing * np.arange(int(np.floor((vmax - vmin) / new_spacing)) + 1)
    return target[::-1] if coord[0] > coord[-1] else target


def _qmap(values, model, ref):
    """One cell: ``F_ref^-1(F_model(values))`` with empirical CDFs.

    ``model`` and ``ref`` are the cell's climatology samples (any shape,
    pooled); ``values`` keeps its shape.
    """
    import numpy as np

    values = np.asarray(values, dtype=float)
    model, ref = np.ravel(model).astype(float), np.ravel(ref).astype(float)
    out = np.full(values.shape, np.nan, dtype=float)
    ok = ~np.isnan(values)
    sorted_m, sorted_r = np.sort(model[~np.isnan(model)]), np.sort(ref[~np.isnan(ref)])
    if not ok.any() or not sorted_m.size or not sorted_r.size:
        return out
    # Mid-rank: a value tied with many model values (precip zeros) sits in the
    # middle of its tie block. Taking the top of the block (side="right") put
    # a dry forecast at the model's dry fraction and mapped it to observed rain.
    left = np.searchsorted(sorted_m, values[ok], side="left")
    right = np.searchsorted(sorted_m, values[ok], side="right")
    quants = np.clip((left + right) / (2 * sorted_m.size), 0.0, 1.0)
    out[ok] = np.interp(quants, (np.arange(sorted_r.size) + 0.5) / sorted_r.size, sorted_r)
    return out


@weather_skill(
    name="downscale",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--algorithm",
    required=True,
    choices=["linear-interpolation", "q-q"],
)
@weather_skill.argument("-f", "--factor", type=int)
@weather_skill.argument("--target-resolution", type=float)
@weather_skill.argument("--reference-grid")
@weather_skill.argument(
    "--qq-reference",
    help="q-q: observed climatology sample on the output grid (the target distribution).",
)
@weather_skill.argument(
    "--qq-model",
    help=(
        "q-q: the model's own climatology sample (hindcasts / reforecasts) on the "
        "input grid. Without it the input is its own model sample."
    ),
)
def downscale(
    ds,
    variable,
    algorithm,
    factor,
    target_resolution,
    reference_grid,
    qq_reference,
    qq_model,
    **kwargs,
):
    """Put a gridded Zarr onto a finer or equally spaced lat/lon grid, optionally with quantile mapping."""
    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401

    n = sum(x is not None for x in (factor, target_resolution, reference_grid))
    if n != 1:
        raise UsageError("exactly one of --factor, --target-resolution, --reference-grid")
    if algorithm == "q-q" and not qq_reference:
        raise UsageError("--algorithm q-q requires --qq-reference")
    if (qq_reference or qq_model) and algorithm != "q-q":
        raise UsageError("--qq-reference / --qq-model only valid with --algorithm q-q")

    lat_dim, lon_dim = detect_spatial_dims(ds)
    if variable:
        ds = ds[[variable]]
    ds = ensure_normalized_longitude(ds, lon_dim)

    # q-q inputs are checked before the (slow) interpolation. Each cell's sample
    # is every value along the non-spatial dims (years, days, members), pooled.
    samples = {}
    if algorithm == "q-q":
        for flag, path in (("--qq-reference", qq_reference), ("--qq-model", qq_model)):
            if path is None:
                continue
            other = xr.open_zarr(path, consolidated=False)
            missing = [v for v in ds.data_vars if v not in other.data_vars]
            if missing:
                raise UsageError(
                    f"{flag} has no variable {missing} (has {list(other.data_vars)}); "
                    "give one side the other's name with edit-coords --rename, or limit "
                    "the input with --variable"
                )
            for v in ds.data_vars:
                want = ds[v].attrs.get("units") or format(ds[v].pint.units or "", "~cf")
                have = other[v].attrs.get("units")
                if not units_equal(have, want):
                    raise UsageError(
                        f"{flag} {v!r} is in {have!r} but the input is in {want!r}; "
                        "unit-convert one side first"
                    )
                if not set(other[v].dims) - {lat_dim, lon_dim}:
                    raise UsageError(
                        f"{flag} {v!r} has only {list(other[v].dims)}: q-q needs a "
                        "climatology sample dim besides lat/lon (e.g. time over many "
                        "years, or number)"
                    )
            samples[flag] = other
        if qq_model is None:
            for v in ds.data_vars:
                if not set(ds[v].dims) - {lat_dim, lon_dim}:
                    raise UsageError(
                        f"--input {v!r} has no sample dim besides lat/lon and no "
                        "--qq-model was given; pass the model climatology with --qq-model"
                    )
            print(
                "Warning: no --qq-model, so the input is its own model climatology and "
                "the output takes the --qq-reference distribution: a single forecast "
                "becomes the reference climatology. Pass --qq-model <hindcasts.zarr> "
                "to correct a forecast.",
                file=sys.stderr,
            )
    in_lat, in_lon = _spacing(ds, lat_dim), _spacing(ds, lon_dim)
    if not (in_lat > 0 and in_lon > 0):
        raise UsageError(
            f"input grid spacing is zero (lat {in_lat}, lon {in_lon}); each latitude and "
            "longitude must appear once"
        )

    if reference_grid is not None:
        ref = xr.open_zarr(reference_grid, consolidated=False)
        ref_lat_dim, ref_lon_dim = detect_spatial_dims(ref)
        ref = ensure_normalized_longitude(ref, ref_lon_dim)
        ref_lat, ref_lon = _spacing(ref, ref_lat_dim), _spacing(ref, ref_lon_dim)
        # Only reference points within half an input cell of the input's span
        # (as coarsen does): a continental reference would otherwise write a
        # mostly empty grid. 1e-6 slack for float32 coords.
        new_lat, new_lon = (
            np.asarray(ref[rd].values)[
                (ref[rd].values >= float(ds[d].min()) - sp / 2 - 1e-6)
                & (ref[rd].values <= float(ds[d].max()) + sp / 2 + 1e-6)
            ]
            for rd, d, sp in ((ref_lat_dim, lat_dim, in_lat), (ref_lon_dim, lon_dim, in_lon))
        )
        if not new_lat.size or not new_lon.size:
            raise UsageError("--reference-grid does not overlap the input's lat/lon span")
        # 1% rel_tol: float noise / half-cell shift is not a resolution change.
        if (in_lat < ref_lat and not math.isclose(in_lat, ref_lat, rel_tol=1e-2)) or (
            in_lon < ref_lon and not math.isclose(in_lon, ref_lon, rel_tol=1e-2)
        ):
            raise UsageError("--reference-grid is coarser than input; use coarsen")
    else:
        if factor is not None:
            if factor < 1:
                raise UsageError("--factor must be >= 1")
            # Split each input cell into `factor` steps between the input's own
            # points, so every factor-th output point is an input point and
            # --factor 1 keeps the grid. Stepping by a measured spacing instead
            # drifts on float32 grids such as CHIRPS.
            new_lat, new_lon = (
                np.linspace(c[0], c[-1], (c.size - 1) * factor + 1)
                for c in (ds[lat_dim].values.astype(float), ds[lon_dim].values.astype(float))
            )
        else:
            too_coarse = (
                in_lat < target_resolution
                and not math.isclose(in_lat, target_resolution, rel_tol=1e-2)
            ) or (
                in_lon < target_resolution
                and not math.isclose(in_lon, target_resolution, rel_tol=1e-2)
            )
            if target_resolution <= 0 or too_coarse:
                raise UsageError("--target-resolution must be finer-or-equal to input")
            new_lat = _target_coord(ds[lat_dim].values, target_resolution)
            new_lon = _target_coord(ds[lon_dim].values, target_resolution)

    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )
    out = ds.regrid.linear(target)

    if algorithm == "q-q":
        ref = samples["--qq-reference"]
        ref = ensure_normalized_longitude(ref, lon_dim)
        # The reference must hold every output grid point (it may extend past
        # them, e.g. the same continental grid passed as --reference-grid).
        try:
            ref = ref.sel(
                {lat_dim: out[lat_dim], lon_dim: out[lon_dim]}, method="nearest", tolerance=1e-6
            )
        except KeyError:
            raise UsageError(
                "--qq-reference must contain every post-downscale grid point; build it on "
                "the output grid (or pass it as --reference-grid too)"
            ) from None
        ref = ref.assign_coords({lat_dim: out[lat_dim], lon_dim: out[lon_dim]})
        model = out
        if qq_model is not None:
            model = samples["--qq-model"]
            model = ensure_normalized_longitude(model, lon_dim)[list(out.data_vars)]
            model = model.regrid.linear(target)
        for v in out.data_vars:
            # Distinct core-dim names so the three samples never broadcast together.
            core = {
                name: [d for d in da.dims if d not in (lat_dim, lon_dim)]
                for name, da in (("in", out[v]), ("model", model[v]), ("ref", ref[v]))
            }
            mapped = xr.apply_ufunc(
                _qmap,
                out[v].load(),
                model[v].load().rename({d: f"_qq_model_{d}" for d in core["model"]}),
                ref[v].load().rename({d: f"_qq_ref_{d}" for d in core["ref"]}),
                input_core_dims=[
                    core["in"],
                    [f"_qq_model_{d}" for d in core["model"]],
                    [f"_qq_ref_{d}" for d in core["ref"]],
                ],
                output_core_dims=[core["in"]],
                vectorize=True,
                output_dtypes=[float],
            )
            mapped.attrs = dict(out[v].attrs)
            out[v] = mapped.transpose(*out[v].dims)
    return out


if __name__ == "__main__":
    downscale()

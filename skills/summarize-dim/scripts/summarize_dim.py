# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/weather-skills/weather-skills-core@dev",
# ]
# ///
"""Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""

import itertools

import numpy as np
import xarray as xr
from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import latitude_weights

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"


@weather_skill(
    name="summarize-dim",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dim",
    action="append",
    help="Dim to summarize (repeatable).",
)
@weather_skill.argument(
    "--by",
    action="append",
    help="Label coord to group by; its dims collapse to one value per label (repeatable).",
)
@weather_skill.argument(
    "--method",
    required=True,
    choices=["mean", "std", "min", "max", "sum", "median", "quantile"],
)
@weather_skill.argument(
    "--quantile",
    "-q",
    type=float,
    action="append",
    help="Quantile between 0 and 1 for --method quantile (repeatable: 0.1 and 0.9 for a band).",
)
@weather_skill.argument(
    "--lat-weighted",
    action="store_true",
    help="cos(lat) weights for --method mean over latitude.",
)
def summarize_dim(ds, variable, dim, by, method, quantile, lat_weighted, **kwargs):
    """Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""
    if not dim and not by:
        raise UsageError("pass --dim, --by, or both")
    dims = list(dict.fromkeys(dim or []))
    for d in dims:
        if d not in ds.dims:
            raise UsageError(f"--dim {d!r} not in dataset (have {list(ds.dims)})")
    by = list(dict.fromkeys(by or []))
    for b in by:
        if b not in ds.coords or b in ds.dims:
            labels = [c for c in ds.coords if c not in ds.dims]
            raise UsageError(f"--by {b!r} is not a label coord (have {labels})")
    if by and lat_weighted:
        raise UsageError("--lat-weighted cannot be combined with --by")
    if (method == "quantile") != bool(quantile):
        raise UsageError("--quantile goes with --method quantile, which needs at least one")
    quantile = list(dict.fromkeys(quantile or []))
    if any(not 0 <= q <= 1 for q in quantile):
        raise UsageError(f"--quantile must be between 0 and 1, got {quantile}")
    if by and len(quantile) > 1:
        raise UsageError("--by takes one --quantile; run once per quantile")
    # The dims a label spans collapse into one entry per label.
    group_dims = list(dict.fromkeys(d for b in by for d in ds[b].dims))
    dims = list(dict.fromkeys([*group_dims, *dims]))
    # Other label coords on the grouped dims (admin_1_name, ...) cannot survive the collapse.
    ds = ds.drop_vars(
        [c for c in ds.coords if c not in (*by, *ds.dims) and set(ds[c].dims) & set(group_dims)]
    )
    # Groups, built once for every variable. Labels on the same dims are matched together;
    # labels on different dims (a county and a year) combine as a product. Each group keeps
    # only the rows/columns/times it touches, plus a mask when that box holds other cells,
    # so no step copies more than one group.
    label_values = {}
    groups = []
    for dset in dict.fromkeys(ds[b].dims for b in by):
        names = [b for b in by if ds[b].dims == dset]
        for b in by:
            if b not in names and set(ds[b].dims) & set(dset):
                raise UsageError(f"--by {b!r} and {names[0]!r} share some but not all dims")
        for b in names:
            values = np.unique(ds[b].values)
            label_values[b] = values[values != ""] if values.dtype.kind in "UO" else values
            if label_values[b].size == 0:
                raise DataError(f'--by {b!r}: every value is the empty label ""; nothing to group')
        entries = []
        for combo in itertools.product(*(range(label_values[b].size) for b in names)):
            mask = np.ones([ds.sizes[d] for d in dset], dtype=bool)
            for b, i in zip(names, combo, strict=True):
                mask &= ds[b].values == label_values[b][i]
            if not mask.any():
                continue
            index = [
                np.nonzero(mask.any(axis=tuple(a for a in range(mask.ndim) if a != axis)))[0]
                for axis in range(mask.ndim)
            ]
            box = mask[np.ix_(*index)]
            entries.append(
                (
                    dict(zip(names, combo, strict=True)),
                    dict(zip(dset, index, strict=True)),
                    None if box.all() else xr.DataArray(box, dims=dset),
                )
            )
        groups.append(entries)

    lat_dim = None
    if lat_weighted:
        if method != "mean":
            raise UsageError("--lat-weighted requires --method mean")
        lat_dim, _ = detect_spatial_dims(ds)
        if lat_dim not in dims:
            raise UsageError(f"--lat-weighted needs --dim {lat_dim}")

    # No --variable: whole dataset (vars without the dim are skipped by rdims).
    selected = list(dict.fromkeys(variable)) if variable else list(ds.data_vars)
    for var in selected:
        if var not in ds.data_vars:
            raise UsageError(f"--variable {var!r} not in dataset (have {list(ds.data_vars)})")
    out = ds.drop_vars(by)
    for var in selected:
        da = ds[var]
        rdims = [d for d in dims if d in da.dims]
        # With --by, only variables carrying every label dim are summarized.
        if not rdims or not set(group_dims) <= set(da.dims):
            continue
        if by:
            # min_count=1 / ddof=1 as below. Combinations with no cells stay NaN.
            kw = {"min_count": 1} if method == "sum" else {"ddof": 1} if method == "std" else {}
            if method == "quantile":
                kw = {"q": quantile[0]}
            kept = [d for d in da.dims if d not in rdims]
            units = da.pint.units
            da = da.pint.dequantify()  # values are filled into numpy; units go back on below
            values = None
            for parts in itertools.product(*groups):
                index = {d: i for _, isel, _ in parts for d, i in isel.items()}
                sub = da.isel(index)
                for _, _, box in parts:
                    if box is not None:
                        sub = sub.where(box)
                reduced = getattr(sub, method)(dim=rdims, keep_attrs=True, **kw)
                if values is None:
                    template = reduced
                    shape = [da.sizes[d] for d in kept] + [label_values[b].size for b in by]
                    values = np.full(shape, np.nan, dtype=np.result_type(reduced.dtype, np.float32))
                where = {b: i for labels, _, _ in parts for b, i in labels.items()}
                values[(..., *(where[b] for b in by))] = reduced.transpose(*kept).values
            out[var] = xr.DataArray(
                values,
                dims=[*kept, *by],
                coords={
                    **{c: v for c, v in template.coords.items() if set(v.dims) <= set(kept)},
                    **{b: label_values[b] for b in by},
                },
                attrs=template.attrs,
            )
            if units is not None:
                out[var] = out[var].copy(data=out[var].data * units)
        elif method == "sum":
            # min_count=1: all-NaN → NaN, not 0
            out[var] = da.sum(dim=rdims, keep_attrs=True, min_count=1)
        elif method == "std":
            out[var] = da.std(dim=rdims, keep_attrs=True, ddof=1)
        elif method == "quantile":
            # One quantile leaves a scalar quantile coord; several add a quantile dim.
            q = quantile[0] if len(quantile) == 1 else quantile
            out[var] = da.pint.dequantify().quantile(q, dim=rdims, keep_attrs=True)
            if da.pint.units is not None:
                out[var] = out[var].copy(data=out[var].data * da.pint.units)
        elif method == "mean" and lat_weighted and lat_dim in rdims:
            out[var] = (
                da.pint.dequantify()
                .weighted(latitude_weights(ds[lat_dim]))
                .mean(dim=rdims, keep_attrs=True)
            )
            if da.pint.units is not None:
                out[var] = out[var].copy(data=out[var].data * da.pint.units)
        else:
            out[var] = getattr(da, method)(dim=rdims, keep_attrs=True)
        if method == "quantile":
            # xarray's quantile drops coords the reduction leaves alone (a forecast's
            # scalar init time); keep them, as the other statistics do.
            out[var] = out[var].assign_coords(
                {
                    c: v
                    for c, v in ds[var].coords.items()
                    if c not in out[var].coords and not set(v.dims) & {*rdims, *by}
                }
            )
        # Record the statistic (CF cell_methods). A spread or a sum is not the
        # input quantity, so its long_name must not read "Total precipitation".
        attrs = dict(out[var].attrs)
        cf_method = {"std": "standard_deviation", "min": "minimum", "max": "maximum"}
        step = f"{': '.join(rdims)}: {cf_method.get(method, method)}"
        if method == "quantile":
            step += f" {', '.join(f'{q:g}' for q in quantile)}"
        if by:
            step += f" (grouped by {', '.join(by)})"
        attrs["cell_methods"] = (
            f"{attrs['cell_methods']} {step}" if attrs.get("cell_methods") else step
        )
        if method in ("std", "sum"):
            label = "Standard deviation" if method == "std" else "Sum"
            what = attrs.get("long_name") or attrs.get("standard_name") or var
            attrs["long_name"] = f"{label} of {what} across {', '.join(rdims)}"
            # standard_name stays: CF encodes a spread as the quantity's
            # standard_name plus a cell_methods statistic.
            for key in ("GRIB_name", "short_name", "comment"):
                attrs.pop(key, None)
        out[var].attrs = attrs

    for d in dims:
        if d in out.dims and all(d not in out[v].dims for v in out.data_vars):
            out = out.drop_dims(d)
    return out


if __name__ == "__main__":
    summarize_dim()

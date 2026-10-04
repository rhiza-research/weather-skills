# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
# ]
# ///
"""Summarize named dims with a statistic."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import latitude_weights
from weather_skills_core.units import ureg

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _weighted_mean(da, weights, dims):
    """Weighted mean on magnitudes, units re-attached.

    xarray's weighted mean is ``sum(w * x) / sum(w)``. pint refuses that for
    offset units (``degree_Celsius``) even though the result — a convex
    combination — is a well-defined temperature in the same unit. Strip the
    quantity, average the magnitudes, and re-quantify with the input unit.
    """
    units = da.pint.units
    if units is None:
        return da.weighted(weights).mean(dim=dims, keep_attrs=True)
    plain = da.copy(data=da.pint.magnitude)
    mean = plain.weighted(weights).mean(dim=dims, keep_attrs=True)
    # Re-wrap the data only: .pint.quantify() would also re-quantify coords.
    return mean.copy(data=ureg.Quantity(mean.data, units))


@weather_skill(
    name="summarize-dim",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dim",
    action="append",
    required=True,
    help="Dim to summarize (repeatable).",
)
@weather_skill.argument(
    "--method",
    required=True,
    choices=["mean", "std", "min", "max", "sum", "median"],
)
@weather_skill.argument(
    "--lat-weighted",
    action="store_true",
    help="cos(lat) weights for --method mean over latitude.",
)
def summarize_dim(ds, variable, dim, method, lat_weighted, **kwargs):
    """Summarize named dims with a statistic."""
    dims = list(dict.fromkeys(dim))
    lat_dim = None
    if lat_weighted:
        if method != "mean":
            raise UsageError("--lat-weighted requires --method mean")
        lat_dim, _ = detect_spatial_dims(ds)
        if lat_dim not in dims:
            raise UsageError(f"--lat-weighted needs --dim {lat_dim}")

    # No --variable: whole dataset (vars without the dim are skipped by rdims).
    selected = list(dict.fromkeys(variable)) if variable else list(ds.data_vars)
    out = ds.copy()
    for var in selected:
        da = ds[var]
        rdims = [d for d in dims if d in da.dims]
        if not rdims:
            continue
        if method == "median" and da.chunks is not None and set(rdims) == set(da.dims):
            da = da.load()
        if method == "sum":
            # min_count=1: all-NaN → NaN, not 0
            out[var] = da.sum(dim=rdims, keep_attrs=True, min_count=1)
        elif method == "std":
            out[var] = da.std(dim=rdims, keep_attrs=True, ddof=1)
        elif method == "mean" and lat_weighted and lat_dim in rdims:
            out[var] = _weighted_mean(da, latitude_weights(ds[lat_dim]), rdims)
        else:
            out[var] = getattr(da, method)(dim=rdims, keep_attrs=True)

    for d in dims:
        if d in out.dims and all(d not in out[v].dims for v in out.data_vars):
            out = out.drop_dims(d)
    return out


if __name__ == "__main__":
    summarize_dim()

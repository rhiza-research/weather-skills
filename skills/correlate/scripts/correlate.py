# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
#   "scipy",
# ]
# ///
"""Correlate two Zarrs along a dimension, cell by cell: Pearson or Spearman r, a p-value, and the pair count."""

import numpy as np
import xarray as xr
from scipy import stats
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_utils import ensure_normalized_longitude

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="correlate",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument(
    "--variable-a", help="Variable in the first --input. Default: its first usable variable."
)
@weather_skill.argument(
    "--variable-b", help="Variable in the second --input. Default: its first usable variable."
)
@weather_skill.argument(
    "--dim",
    action="append",
    help="Dimension to correlate along (repeatable). Default: time.",
)
@weather_skill.argument("--method", choices=["pearson", "spearman"], default="pearson")
def correlate(ds, variable_a, variable_b, dim, method, **kwargs):
    """Correlate two Zarrs along a dimension, cell by cell: Pearson or Spearman r, a p-value, and the pair count."""
    if len(ds) != 2:
        raise UsageError(f"expected exactly two --input paths, got {len(ds)}")
    dims = list(dict.fromkeys(dim or ["time"]))
    pair = []
    for k, (data, name) in enumerate(zip(ds, (variable_a, variable_b), strict=True)):
        label = "ab"[k]
        name = name or auto_variable(data)
        if not name or name not in data:
            raise UsageError(
                f"--variable-{label} {name!r} missing from input {label.upper()}. "
                f"Available: {list(data.data_vars)}"
            )
        da = ensure_normalized_longitude(data)[name]
        if da.pint.units is not None:
            da = da.pint.dequantify()
        if "number" in da.dims and "number" not in dims:
            raise UsageError(
                f"input {label.upper()} still has ensemble members; take the ensemble "
                "mean first (summarize-dim --dim number --method mean)."
            )
        missing = [d for d in dims if d not in da.dims]
        if missing:
            raise UsageError(
                f"input {label.upper()} {name!r} has no {missing} dim (dims: {list(da.dims)})."
            )
        pair.append(da)
    a, b = pair
    only_a = [d for d in a.dims if d not in b.dims]
    only_b = [d for d in b.dims if d not in a.dims]
    if only_a and only_b:
        raise UsageError(
            f"A has dims {only_a} that B lacks and B has {only_b} that A lacks; put both "
            "on the same axes first (e.g. step-to-time, or select one entry)."
        )
    a, b = xr.align(a, b, join="inner")
    empty = [d for d, size in a.sizes.items() if size == 0]
    if empty:
        raise UsageError(
            f"no overlapping {empty} labels between the inputs. Labels must match exactly: "
            "for lat/lon put both on one grid (coarsen --reference-grid); for time "
            "aggregate both with the same bins."
        )

    a, b = xr.broadcast(a, b)
    valid = a.notnull() & b.notnull()
    a, b = a.where(valid), b.where(valid)
    if method == "spearman":
        # Rank only the pairs both inputs have; ties share their mean rank.
        a, b = (
            xr.apply_ufunc(
                lambda x: stats.rankdata(x, axis=-1, nan_policy="omit"),
                v.stack(_pairs=dims),
                input_core_dims=[["_pairs"]],
                output_core_dims=[["_pairs"]],
            ).unstack("_pairs")
            for v in (a, b)
        )
    n = valid.sum(dims)
    r = xr.corr(a, b, dim=dims)
    r = r.where(n >= 3)
    # Two-sided p-value from t = r sqrt((n-2)/(1-r²)), n-2 degrees of freedom.
    t = r * np.sqrt((n - 2).clip(min=0) / np.maximum(1 - r**2, 1e-12))
    p = xr.apply_ufunc(lambda t, df: 2 * stats.t.sf(np.abs(t), df), t, n - 2)

    along = ", ".join(dims)
    r = r.astype("float32")
    r.attrs = {
        "long_name": f"{method.capitalize()} correlation along {along}",
        "units": "1",
        "correlation_method": method,
        "correlation_dims": along,
    }
    p = p.astype("float32").where(r.notnull())
    p.attrs = {"long_name": "Two-sided p-value of the correlation", "units": "1"}
    n = n.astype("int32")
    n.attrs = {"long_name": f"Pairs with both values along {along}", "units": "1"}
    out = xr.Dataset({"correlation": r, "p_value": p, "n_pairs": n})

    finite = r.notnull()
    if not bool(finite.any()):
        summary = "r n/a  (no cell has 3 or more pairs)"
    elif r.size == 1:
        summary = f"r {float(r):+.2f}  p {float(p):.3g}  n {int(n)}"
    else:
        summary = (
            f"mean r {float(r.mean()):+.2f}  range {float(r.min()):+.2f} … "
            f"{float(r.max()):+.2f}  ({int(finite.sum())} cells)"
        )
    print(f"correlate  {summary}")
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["correlate_summary"] = summary
    return out


if __name__ == "__main__":
    correlate()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scipy",
#   "weather-skills-core>=0.1.1",
# ]
# ///
"""Fit y = intercept + slope * x by least squares along one dim, with a prediction interval."""

import sys

import numpy as np
import xarray as xr
from scipy.stats import t as student_t
from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

OUTPUT_VARS = ("fit", "fit_lower", "fit_upper", "slope", "intercept", "r2", "n", "residual_std")


@weather_skill(
    name="linear-fit",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--x", required=True, help="Predictor variable.")
@weather_skill.argument("--y", required=True, help="Predicted variable.")
@weather_skill.argument("--dim", required=True, help="Dim whose entries are the samples.")
@weather_skill.argument(
    "--interval",
    type=float,
    default=0.8,
    help="Prediction-interval probability, 0-1 (default 0.8).",
)
def linear_fit(ds, x, y, dim, interval, **kwargs):
    """Fit y = intercept + slope * x by least squares along one dim, with a prediction interval."""
    for var in (x, y):
        if var not in ds.data_vars:
            raise UsageError(f"--x/--y {var!r} not in dataset (have {list(ds.data_vars)})")
        if dim not in ds[var].dims:
            raise UsageError(f"--dim {dim!r} not a dim of {var!r} (has {list(ds[var].dims)})")
    if x == y:
        raise UsageError("--x and --y must be different variables")
    if not 0 < interval < 1:
        raise UsageError(f"--interval must be between 0 and 1, got {interval}")
    clash = [v for v in OUTPUT_VARS if v in ds.variables]
    if clash:
        raise UsageError(f"input already has {clash}; rename them first with edit-coords")

    x_units = ds[x].pint.units
    y_units = ds[y].pint.units
    xv = ds[x].pint.dequantify() if x_units is not None else ds[x]
    yv = ds[y].pint.dequantify() if y_units is not None else ds[y]

    # Fit only on samples where both are present; nothing is filled.
    paired = xv.notnull() & yv.notnull()
    xp = xv.where(paired)
    yp = yv.where(paired)
    n = paired.sum(dim)
    x_mean = xp.mean(dim)
    y_mean = yp.mean(dim)
    sxx = ((xp - x_mean) ** 2).sum(dim)
    sxy = ((xp - x_mean) * (yp - y_mean)).sum(dim)
    syy = ((yp - y_mean) ** 2).sum(dim)
    # Fewer than 2 samples, or x without spread, has no slope.
    slope = (sxy / sxx).where((n >= 2) & (sxx > 0))
    intercept = y_mean - slope * x_mean
    sse = ((yp - (intercept + slope * xp)) ** 2).sum(dim)
    r2 = (1 - sse / syy).where(syy > 0)
    # Residual std and the interval need n >= 3 (n - 2 degrees of freedom).
    residual_std = np.sqrt(sse / (n - 2)).where(n >= 3)
    t_crit = xr.apply_ufunc(student_t.ppf, (1 + interval) / 2, (n - 2).where(n >= 3))

    # Every x gets a fitted value, including x whose y is missing (a forecast year).
    fit = intercept + slope * xv
    half_width = t_crit * residual_std * np.sqrt(1 + 1 / n + (xv - x_mean) ** 2 / sxx)

    y_text = format(y_units, "cf") if y_units is not None else None
    if x_units is not None and y_units is not None:
        slope_units = format(y_units / x_units, "cf")
    else:
        slope_units = None
    pct = f"{interval * 100:g}%"
    what = f"{y} on {x} along {dim}"
    new = {
        "fit": (fit, y_text, f"Linear fit of {what}"),
        "fit_lower": (fit - half_width, y_text, f"Lower bound of {pct} prediction interval"),
        "fit_upper": (fit + half_width, y_text, f"Upper bound of {pct} prediction interval"),
        "slope": (slope, slope_units, f"Slope of linear fit of {what}"),
        "intercept": (intercept, y_text, f"Intercept of linear fit of {what}"),
        "r2": (r2, "1", f"Coefficient of determination (R2) of {what}"),
        "n": (n, "1", f"Number of samples with both {x} and {y}"),
        "residual_std": (residual_std, y_text, f"Residual standard error of {what}"),
    }
    out = ds.copy()
    for name, (da, units, long_name) in new.items():
        da = da.astype("float64") if name != "n" else da.astype("int64")
        da.attrs = {"long_name": long_name}
        if units is not None:
            da.attrs["units"] = units
        if name in ("fit_lower", "fit_upper"):
            da.attrs["prediction_interval"] = interval
        out[name] = da

    if slope.ndim == 0:
        print(
            f"{y} = {float(intercept):.4g} + {float(slope):.4g} * {x}; "
            f"R2={float(r2):.3f}, n={int(n)}, residual std={float(residual_std):.4g}",
            file=sys.stderr,
        )
    else:
        print(
            f"fit per {dict(slope.sizes)}; median R2={float(r2.median()):.3f}",
            file=sys.stderr,
        )
    return out


if __name__ == "__main__":
    linear_fit()

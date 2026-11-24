# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Blend several forecasts into one with fixed or fitted weights: weighted, linear, or logistic."""

from pathlib import Path

import numpy as np
import pint
import pint_xarray
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.decorator import INPUT_PATH_ATTR
from weather_skills_core.units import dequantify_dataset, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_MODELS = ("weighted", "linear", "logistic")


@weather_skill(
    name="mme-blend",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--forecast",
    type=Dataset("any"),
    action="append",
    required=True,
    help="Forecast Zarr, one per source (repeat). Each is one predictor.",
)
@weather_skill.argument(
    "--names",
    help="Comma-separated predictor names, one per --forecast (default: file stems).",
)
@weather_skill.argument("--variable", "-v", help="Variable to read from every --forecast.")
@weather_skill.argument("--weights", type=Dataset("any"), help="Weights Zarr (coef, intercept).")
@weather_skill.argument(
    "--model",
    choices=list(_MODELS),
    help="Model for --coef weights; read from the weights Zarr otherwise.",
)
@weather_skill.argument(
    "--coef",
    action="append",
    metavar="NAME=W",
    help="Fixed weight for one predictor (repeat), instead of --weights.",
)
@weather_skill.argument(
    "--intercept", type=float, help="Intercept for --coef weights (linear/logistic)."
)
def mme_blend(forecast, names, variable, weights, model, coef, intercept, **kwargs):
    """Blend several forecasts into one with fixed or fitted weights: weighted, linear, or logistic."""
    if (weights is None) == (not coef):
        raise UsageError("pass either --weights or --coef (with --model), not both or neither")

    # ── Weights: from a Zarr written by a fit, or fixed on the command line ──
    if weights is not None:
        if intercept is not None:
            raise UsageError("--intercept goes with --coef; the weights Zarr holds its own")
        stored = weights.attrs.get("model")
        if stored not in _MODELS:
            raise UsageError(f"--weights has model attr {stored!r}; expected one of {_MODELS}")
        if model is not None and model != stored:
            raise UsageError(f"--model {model} but the weights were fitted as {stored}")
        model = stored
        if "coef" not in weights or "predictor" not in weights["coef"].dims:
            raise UsageError("--weights needs a 'coef' variable with a 'predictor' dim")
        w = weights["coef"]
        w = w.pint.dequantify() if w.pint.units is not None else w
        b = weights.get("intercept")
        if b is not None and b.pint.units is not None:
            b = b.pint.dequantify()
        if model != "weighted" and b is None:
            raise UsageError(f"--weights model {model} needs an 'intercept' variable")
        target_units = weights.attrs.get("predictor_units")
    else:
        if model is None:
            raise UsageError("--coef needs --model weighted, linear, or logistic")
        pairs = {}
        for item in coef:
            key, sep, value = item.partition("=")
            try:
                pairs[key.strip()] = float(value)
            except ValueError:
                sep = ""
            if not sep or not key.strip():
                raise UsageError(f"--coef expects NAME=W, got {item!r}")
        if model == "weighted" and intercept not in (None, 0.0):
            raise UsageError("--model weighted has no intercept; use --model linear")
        w = xr.DataArray(list(pairs.values()), dims="predictor", coords={"predictor": list(pairs)})
        b = None if model == "weighted" else xr.DataArray(intercept or 0.0)
        target_units = None

    # ── Forecasts: one predictor each ──
    if names:
        labels = [n.strip() for n in names.split(",")]
        if len(labels) != len(forecast):
            raise UsageError(f"--names has {len(labels)} names for {len(forecast)} --forecast")
    else:
        labels = [Path(ds.attrs[INPUT_PATH_ATTR]).stem for ds in forecast]
    if len(set(labels)) != len(labels):
        raise UsageError(f"predictor names repeat: {labels}; pass --names")

    predictors = [str(p) for p in w["predictor"].values]
    needed = sorted({part for p in predictors for part in p.split(":")})
    if sorted(labels) != needed:
        raise UsageError(
            f"forecast names {sorted(labels)} do not match the weights' predictors {needed}; "
            "pass --names in the order of --forecast"
        )

    columns = {}
    for label, ds in zip(labels, forecast, strict=True):
        name = variable or auto_variable(ds)
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --forecast {label}. Available: {list(ds.data_vars)}"
            )
        da = ds[name]
        if "number" in da.dims:
            # A NaN member leaves the mean NaN (no mean over the members left).
            da = da.mean("number", skipna=False, keep_attrs=True)
        columns[label] = da

    # Weighted and linear blends add the forecasts' values, so put them on one unit:
    # the unit the weights were fitted in, else the first forecast's.
    units = None
    if model != "logistic":
        first = columns[labels[0]]
        # The units string as written on output (mm day-1, not pint's millimeter / day).
        units = target_units or (
            dequantify_dataset(first.to_dataset(name="f"))["f"].attrs.get("units")
            if first.pint.units is not None
            else variable_units(first)
        )
        for label, da in columns.items():
            u = variable_units(da)
            if bool(u) != bool(units):
                raise UsageError(
                    f"--forecast {label} units={u!r} but the blend is in {units!r}; "
                    "every forecast needs units (or none)"
                )
            if u and da.pint.units is not None:
                try:
                    columns[label] = da.pint.to(units)
                except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
                    raise UsageError(
                        f"--forecast {label} ({u}) cannot be converted to {units}; put every "
                        "forecast on one basis first"
                    ) from None
    for label, da in columns.items():
        da = da.pint.dequantify() if da.pint.units is not None else da
        columns[label] = da.astype("float64")

    # One column per predictor; an interaction a:b is the product of its parts.
    X = []
    for p in predictors:
        col = columns[p.split(":")[0]]
        for part in p.split(":")[1:]:
            col = col * columns[part]
        X.append(col.drop_attrs())
    X = xr.concat(X, dim="predictor", join="inner", coords="minimal", compat="override")
    X = X.assign_coords(predictor=predictors)

    eta = (X * w).sum("predictor", skipna=False)
    if b is not None:
        eta = eta + b
    empty = [d for d, size in eta.sizes.items() if size == 0]
    if empty:
        raise UsageError(
            f"no overlapping {', '.join(empty)} labels between the forecasts and the weights"
        )

    if model == "logistic":
        out = (0.5 * (1 + np.tanh(eta / 2))).rename("probability")
        out.attrs = {"long_name": "Blended event probability", "units": "1"}
    else:
        out = eta.rename("blend")
        out.attrs = {"long_name": f"{model.capitalize()} blend of {', '.join(labels)}"}
        if units:
            out.attrs["units"] = units
    out.attrs["mme_model"] = model
    out.attrs["mme_predictors"] = ",".join(predictors)

    result = out.astype("float32").to_dataset()
    result.attrs["mme_model"] = model
    return result


if __name__ == "__main__":
    mme_blend()

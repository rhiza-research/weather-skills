# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/weather-skills/weather-skills-core@dev",
#   "pandas",
# ]
# ///
"""Fit multi-model blend weights from forecasts and observations: weighted, linear, or logistic."""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
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
    name="mme-fit",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--forecast",
    type=Dataset("any"),
    action="append",
    required=True,
    help="Forecast Zarr, one per source (repeat). Each is one predictor.",
)
@weather_skill.argument("--obs", type=Dataset("any"), required=True, help="Ground-truth Zarr.")
@weather_skill.argument(
    "--model",
    choices=list(_MODELS),
    required=True,
    help="weighted (X·coef), linear (X·coef + intercept), or logistic (probability).",
)
@weather_skill.argument(
    "--names",
    help="Comma-separated predictor names, one per --forecast (default: file stems).",
)
@weather_skill.argument("--variable", "-v", help="Variable to read from every --forecast.")
@weather_skill.argument("--obs-variable", help="Variable to read from --obs.")
@weather_skill.argument(
    "--interaction",
    action="append",
    metavar="A:B",
    help="Add the product of named predictors as one more predictor (repeat).",
)
@weather_skill.argument(
    "--fit-along",
    action="append",
    metavar="DIM",
    help="Dim that gets its own weights, one fit per entry (repeat). Default: one fit.",
)
@weather_skill.argument(
    "--alpha", type=float, default=0.0, help="L2 penalty on coef, not intercept (default 0)."
)
@weather_skill.argument(
    "--max-iter", type=int, default=100, help="Logistic Newton iterations (default 100)."
)
@weather_skill.argument(
    "--tol", type=float, default=1e-4, help="Logistic stop: largest coef step (default 1e-4)."
)
def mme_fit(
    forecast,
    obs,
    model,
    names,
    variable,
    obs_variable,
    interaction,
    fit_along,
    alpha,
    max_iter,
    tol,
    **kwargs,
):
    """Fit multi-model blend weights from forecasts and observations: weighted, linear, or logistic."""
    if alpha < 0:
        raise UsageError(f"--alpha must be >= 0, got {alpha}")
    if max_iter < 1 or tol <= 0:
        raise UsageError("--max-iter must be >= 1 and --tol > 0")
    fit_along = fit_along or []

    # ── Forecasts: one predictor each ──
    if names:
        labels = [n.strip() for n in names.split(",")]
        if len(labels) != len(forecast):
            raise UsageError(f"--names has {len(labels)} names for {len(forecast)} --forecast")
    else:
        labels = [Path(ds.attrs[INPUT_PATH_ATTR]).stem for ds in forecast]
    if len(set(labels)) != len(labels) or any(":" in n for n in labels):
        raise UsageError(f"predictor names must be unique and contain no ':' (got {labels})")
    interaction = interaction or []
    for term in interaction:
        unknown = [p for p in term.split(":") if p not in labels]
        if len(term.split(":")) < 2 or unknown:
            raise UsageError(f"--interaction {term!r} must join 2+ of the names {labels}")

    obs_name = obs_variable or auto_variable(obs)
    if not obs_name or obs_name not in obs:
        raise UsageError(
            f"variable {obs_name!r} missing from --obs. Available: {list(obs.data_vars)}"
        )
    truth = obs[obs_name]
    if "number" in truth.dims:
        truth = truth.mean("number", skipna=False, keep_attrs=True)
    if "time" not in truth.dims:
        raise UsageError(f"--obs needs a time axis of valid dates (has {list(truth.dims)})")
    space = [d for d in truth.dims if d != "time"]

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
        if "time" in da.coords and da["time"].ndim == 0:
            da = da.expand_dims("time")
        missing = [d for d in space if d not in da.dims]
        if missing:
            raise UsageError(
                f"--forecast {label} lacks the obs dims {missing} (has {list(da.dims)}). Put "
                "forecasts on the obs layout first: point-value --points <obs.zarr> for stations, "
                "coarsen --reference-grid for grids."
            )
        period, obs_period = (
            da.attrs.get("aggregation_period"),
            truth.attrs.get("aggregation_period"),
        )
        if period and obs_period and period != obs_period:
            raise UsageError(
                f"--forecast {label} covers {period} windows but --obs covers {obs_period}; "
                "aggregate both to the same window"
            )
        for axis in ("latitude", "longitude"):
            if axis in space and da.sizes[axis] > 1 and truth.sizes[axis] > 1:
                fa = float(np.median(np.abs(np.diff(da[axis].values.astype(float)))))
                fb = float(np.median(np.abs(np.diff(truth[axis].values.astype(float)))))
                if not np.isclose(fa, fb, rtol=0.01, atol=1e-6):
                    raise UsageError(
                        f"--forecast {label} grid spacing on {axis} ({fa:g}) differs from --obs "
                        f"({fb:g}); coarsen one onto the other with --reference-grid"
                    )
        columns[label] = da

    # Weighted and linear fits put the forecasts in the obs units; logistic uses them as given.
    # The units string as written on output (mm day-1, not pint's millimeter / day).
    obs_units = dequantify_dataset(truth.to_dataset(name="obs"))["obs"].attrs.get("units")
    if model != "logistic":
        for label, da in columns.items():
            u = variable_units(da)
            if bool(u) != bool(obs_units):
                raise UsageError(
                    f"--forecast {label} units={u!r} but --obs units={obs_units!r}; both need "
                    "units (or neither)"
                )
            if u:
                try:
                    columns[label] = da.pint.to(truth.pint.units)
                except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
                    raise UsageError(
                        f"--forecast {label} ({u}) and --obs ({obs_units}) carry incompatible "
                        "units; put both on one basis first"
                    ) from None
    truth = truth.pint.dequantify() if truth.pint.units is not None else truth
    for label, da in columns.items():
        da = da.pint.dequantify() if da.pint.units is not None else da
        columns[label] = da.astype("float64").drop_attrs()

    # Labels (e.g. station ids) any input has, to report the ones the inner join drops.
    seen = {}
    for da in (truth, *columns.values()):
        for d in space:
            if d in da.indexes and not pd.api.types.is_numeric_dtype(da.indexes[d]):
                seen[d] = seen.get(d, pd.Index([])).union(da.indexes[d])

    # Every forecast on one shared set of inits, leads and cells (or stations).
    aligned = xr.align(*columns.values(), join="inner")
    columns = dict(zip(labels, aligned, strict=True))
    first = aligned[0]
    empty = [d for d, size in first.sizes.items() if size == 0]
    if empty:
        raise UsageError(f"the forecasts share no {', '.join(empty)} labels")

    # ── Ground truth at each forecast's valid date (init + step) ──
    if not truth.indexes["time"].is_unique:
        raise UsageError("--obs time axis has repeated dates")
    if "step" in first.dims:
        valid = first["time"] + first["step"]
        obs_times = pd.DatetimeIndex(truth["time"].values)
        if (obs_times == obs_times.normalize()).all():
            # Daily obs (station days): match on the calendar date, not the init hour.
            valid = valid.dt.floor("D")
        lookup = truth.reindex(time=np.unique(valid.values.ravel()))
        y = lookup.sel(time=xr.DataArray(valid.values, dims=("_init", "_lead")))
        y = y.drop_vars("time").rename(_init="time", _lead="step")
        y = y.assign_coords(time=first["time"].values, step=first["step"].values)
    else:
        y = truth
    y = y.astype("float64").drop_attrs()
    y, *aligned = xr.align(y, *columns.values(), join="inner")
    columns = dict(zip(labels, aligned, strict=True))
    empty = [d for d, size in y.sizes.items() if size == 0]
    if empty or not bool(y.notnull().any()):
        span = f"{first['time'].values.min()} … {first['time'].values.max()}"
        raise UsageError(
            f"no --obs values match the forecasts (empty: {empty or 'none'}; forecast inits "
            f"{span}; obs {truth['time'].values.min()} … {truth['time'].values.max()})"
        )
    for d, index in seen.items():
        dropped = index.difference(pd.Index(y[d].values))
        if len(dropped):
            shown = ", ".join(map(str, dropped[:10])) + (" …" if len(dropped) > 10 else "")
            print(
                f"mme-fit: {len(dropped)} {d} not in every input, left out: {shown}",
                file=sys.stderr,
            )

    # ── Design matrix: one column per predictor, interactions as products ──
    predictors = labels + interaction
    X = []
    for p in predictors:
        col = columns[p.split(":")[0]]
        for part in p.split(":")[1:]:
            col = col * columns[part]
        X.append(col.broadcast_like(y))
    extra = sorted({d for col in X for d in col.dims if d not in y.dims})
    if extra:
        raise UsageError(f"forecasts have dims the obs lack ({extra}); select one entry first")
    X = xr.concat(X, dim="predictor").assign_coords(predictor=predictors)
    unknown = [d for d in fit_along if d not in y.dims]
    if unknown:
        raise UsageError(f"--fit-along {unknown} not among the sample dims {list(y.dims)}")
    pool = [d for d in y.dims if d not in fit_along]
    fit_shape = [y.sizes[d] for d in fit_along]
    n_fit = int(np.prod(fit_shape))
    yv = y.transpose(*fit_along, *pool).values.reshape(n_fit, -1)
    Xv = X.transpose(*fit_along, *pool, "predictor").values.reshape(n_fit, yv.shape[1], -1)
    if model != "weighted":
        Xv = np.concatenate([np.ones_like(Xv[..., :1]), Xv], axis=-1)

    # A sample counts only when the obs and every predictor are present; nothing is filled.
    mask = np.isfinite(yv) & np.isfinite(Xv).all(axis=-1)
    if model == "logistic" and not np.isin(yv[mask], (0.0, 1.0)).all():
        raise UsageError("--model logistic needs a 0/1 --obs (an event mask)")
    Xm = np.where(mask[..., None], Xv, 0.0)
    ym = np.where(mask, yv, 0.0)
    n_samples = mask.sum(axis=1)
    n_params = Xm.shape[-1]
    # The intercept (column 0 when present) is never penalized.
    penalty = alpha * np.eye(n_params)
    if model != "weighted":
        penalty[0, 0] = 0.0
    singular = 1 / np.finfo(float).eps

    n_iter = np.zeros(n_fit, dtype=int)
    if model in ("weighted", "linear"):
        A = np.einsum("fnp,fnq->fpq", Xm, Xm) + penalty
        rhs = np.einsum("fnp,fn->fp", Xm, ym)
        bad = (n_samples == 0) | (np.linalg.cond(A) > singular)
        A[bad] = np.eye(n_params)
        w = np.linalg.solve(A, rhs[..., None])[..., 0]
        w[bad] = np.nan
    else:
        # Newton (IRLS) on log-loss + ½·alpha·‖coef‖², i.e. LogisticRegression(C=1/alpha).
        w = np.zeros((n_fit, n_params))
        active = n_samples > 0
        bad = ~active
        for _ in range(max_iter):
            if not active.any():
                break
            p = 0.5 * (1 + np.tanh(np.einsum("fnp,fp->fn", Xm, w) / 2))
            grad = np.einsum("fnp,fn->fp", Xm, (ym - p) * mask) - w @ penalty
            H = np.einsum("fnp,fn,fnq->fpq", Xm, p * (1 - p) * mask, Xm) + penalty
            stuck = active & (np.linalg.cond(H) > singular)
            # Singular at the start: collinear predictors or too few samples, no fit.
            bad |= stuck & (n_iter == 0)
            n_iter[stuck & (n_iter > 0)] = max_iter
            active &= ~stuck
            H[~active] = np.eye(n_params)
            step = np.linalg.solve(H, grad[..., None])[..., 0]
            step[~active] = 0.0
            w += step
            n_iter[active] += 1
            active &= np.abs(step).max(axis=1) >= tol
        unconverged = (n_iter >= max_iter) & ~bad
        w[bad] = np.nan
        if unconverged.any():
            warnings.warn(
                f"mme-fit: {int(unconverged.sum())} of {n_fit} logistic fits did not converge in "
                f"{max_iter} iterations (perfectly separated events?); raise --max-iter or "
                "--alpha",
                stacklevel=1,
            )

    # ── Weights Zarr ──
    # Coords on the fit dims (station latitude/longitude/name) from the forecasts and obs.
    fit_coords = {}
    for da in (*columns.values(), y):
        for k, c in da.coords.items():
            if c.dims and set(c.dims) <= set(fit_along):
                fit_coords.setdefault(k, c)
    coef_w = w[:, 1:] if model != "weighted" else w
    out = xr.Dataset(coords=fit_coords)
    out["coef"] = xr.DataArray(
        coef_w.reshape(*fit_shape, len(predictors)),
        dims=(*fit_along, "predictor"),
        coords={"predictor": predictors},
        attrs={"long_name": f"{model} blend coefficient per predictor"},
    )
    if model != "weighted":
        out["intercept"] = xr.DataArray(
            w[:, 0].reshape(fit_shape),
            dims=fit_along,
            attrs={
                "long_name": f"{model} blend intercept"
                + (" (logit)" if model == "logistic" else ""),
                **({"units": obs_units} if model == "linear" and obs_units else {}),
            },
        )
    out["n_samples"] = xr.DataArray(
        n_samples.reshape(fit_shape).astype("int32"),
        dims=fit_along,
        attrs={"long_name": "samples with the obs and every predictor present"},
    )
    if model == "logistic":
        out["n_iter"] = xr.DataArray(
            n_iter.reshape(fit_shape).astype("int32"),
            dims=fit_along,
            attrs={"long_name": "Newton iterations (max_iter: not converged)"},
        )
    out.attrs = {
        "model": model,
        "alpha": alpha,
        "fit_along": ",".join(fit_along),
        "pooled_dims": ",".join(pool),
    }
    if model != "logistic" and obs_units:
        out.attrs["predictor_units"] = obs_units

    if n_fit == 1:
        terms = ", ".join(f"{p}={v:.4g}" for p, v in zip(predictors, coef_w[0], strict=True))
        lead = f"intercept={w[0, 0]:.4g}, " if model != "weighted" else ""
        print(f"mme-fit {model}: {lead}{terms}; n={int(n_samples[0])}", file=sys.stderr)
    else:
        print(
            f"mme-fit {model}: {n_fit} fits along {','.join(fit_along)}; "
            f"{int(bad.sum())} without a fit; samples per fit {int(n_samples.min())}–"
            f"{int(n_samples.max())}",
            file=sys.stderr,
        )
    return out


if __name__ == "__main__":
    mme_fit()

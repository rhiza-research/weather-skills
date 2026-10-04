# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Forecast vs observation verification: hits, bias, MAE, RMSE, CRPS, or Brier."""

import sys

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_dataset import ALIASES, MEMBER, PREDICTION_TIMEDELTA
from weather_skills_core.standard_utils import latitude_weights
from weather_skills_core.units import (
    format_units_for_display,
    units_equal,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_METRICS = ("hits", "bias", "mae", "rmse", "crps", "brier")
_VAR = {
    "hits": "event_hit",
    "bias": "bias",
    "mae": "mae",
    "rmse": "rmse",
    "crps": "crps",
    "brier": "brier_score",
}
_ENSEMBLE_METRICS = ("crps", "brier")
_DEFAULT_THRESHOLD = 1.0


def _member_dim(da):
    return next((d for d in da.dims if ALIASES.get(str(d)) == MEMBER), None)


def _lead_dim(da):
    return next((d for d in da.dims if ALIASES.get(str(d)) == PREDICTION_TIMEDELTA), None)


_CRPS_ESTIMATORS = ("fair", "standard")


def crps_ensemble(fc, truth, member, estimator="standard"):
    """Ensemble CRPS = E|X - y| - 0.5 E|X - X'| (Gneiting & Raftery 2007).

    The spread term uses the sorted-member identity
    sum_{i,j} |x_i - x_j| = 2 sum_i (2i - M - 1) x_(i), so memory stays linear in M
    (the pairwise form materialises M^2 copies of the grid).

    estimator="standard" (default) divides by M^2 -- properscoring crps_ensemble, which
    Sheerwater's benchmark and xskillscore use.
    estimator="fair" divides the pair sum by M(M-1) -- unbiased for a finite ensemble
    (Ferro 2014; Zamo & Naveau 2018), and what WeatherBench 2 reports.
    """
    m = fc.sizes[member]
    skill = np.abs(fc - truth).mean(member)
    if fc.chunks is not None:  # lazily opened Zarr: the sort needs all members in one chunk
        fc = fc.chunk({member: -1})
    ranked = xr.apply_ufunc(
        np.sort,
        fc,
        input_core_dims=[[member]],
        output_core_dims=[[member]],
        kwargs={"axis": -1},
        dask="parallelized",
        output_dtypes=[fc.dtype],
    )
    coef = xr.DataArray(2.0 * np.arange(1, m + 1) - m - 1, dims=[member])
    pair_sum_half = (ranked * coef).sum(member)  # = 0.5 * sum_{i,j} |x_i - x_j|
    denom = m * (m - 1) if estimator == "fair" else m * m
    return skill - pair_sum_half / denom


@weather_skill(
    name="verify",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--forecast", type=Dataset("any"), required=True)
@weather_skill.argument("--obs", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--metric",
    choices=list(_METRICS),
    default="hits",
    help=(
        "hits (event classification), bias (forecast − obs), mae, rmse (needs --reduce), "
        "crps (ensemble), or brier (ensemble + --threshold)."
    ),
)
@weather_skill.argument(
    "--threshold",
    type=float,
    default=None,
    help=(
        "Event cutoff: a cell is an event when the variable is >= this. Used by hits "
        f"(default {_DEFAULT_THRESHOLD}) and required for brier."
    ),
)
@weather_skill.argument(
    "--reduce",
    action="append",
    default=None,
    help="Dimension to average the score over (repeatable), e.g. --reduce time. Required for rmse.",
)
@weather_skill.argument(
    "--crps-estimator",
    choices=list(_CRPS_ESTIMATORS),
    default="standard",
    help=(
        "CRPS spread normalisation: standard (M^2; properscoring, as Sheerwater and "
        "xskillscore use) or fair (M(M-1), unbiased; WeatherBench 2)."
    ),
)
def verify(forecast, obs, variable, metric, threshold, reduce, crps_estimator, **kwargs):
    """Forecast vs observation verification: hits, bias, MAE, RMSE, CRPS, or Brier."""
    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )

    fc, truth = forecast[fc_name], obs[obs_name]
    lead = _lead_dim(fc)
    if lead and "time" not in fc.dims and "time" in truth.dims:
        raise UsageError(
            f"forecast still has a lead axis ({lead!r}); run step-to-time before verify "
            "so valid times can align with --obs."
        )
    member = _member_dim(fc)
    if metric in _ENSEMBLE_METRICS and member is None:
        raise UsageError(
            f"--metric {metric} needs an ensemble forecast (a member/number dim); "
            "this forecast is deterministic. Use mae or rmse instead."
        )
    if metric == "brier" and threshold is None:
        raise UsageError("--metric brier needs an explicit --threshold that defines the event.")
    if metric == "rmse" and not reduce:
        raise UsageError(
            "--metric rmse needs --reduce DIM (e.g. --reduce time); per cell it equals |error|."
        )
    if metric == "hits" and reduce:
        raise UsageError("--reduce is not defined for --metric hits (a categorical field).")
    if metric == "crps" and crps_estimator == "fair" and fc.sizes[member] < 2:
        raise UsageError(
            "--crps-estimator fair needs at least 2 members; use --crps-estimator standard."
        )
    for d in reduce or []:
        if d not in fc.dims:
            raise UsageError(f"--reduce {d!r} is not a forecast dim {list(fc.dims)}")
        if d == member:
            raise UsageError(
                f"--reduce {d!r} is the ensemble dim; ensemble handling is set by --metric."
            )
    if metric == "hits" and threshold is None:
        threshold = _DEFAULT_THRESHOLD

    mismatched = []
    for axis, names in (("latitude", ("latitude", "lat")), ("longitude", ("longitude", "lon"))):
        a = next((n for n in names if n in forecast.dims), None)
        b = next((n for n in names if n in obs.dims), None)
        if not a or not b:
            continue
        fa = np.asarray(forecast[a].values, dtype=float)
        fb = np.asarray(obs[b].values, dtype=float)
        if fa.size < 2 or fb.size < 2:
            continue
        if not np.isclose(
            float(np.median(np.abs(np.diff(fa)))),
            float(np.median(np.abs(np.diff(fb)))),
            rtol=0.01,
            atol=1e-6,
        ):
            mismatched.append(axis)
    if mismatched:
        raise UsageError(
            "obs grid spacing does not match the forecast on "
            f"{' and '.join(mismatched)}; coarsen --obs onto the forecast "
            "with --reference-grid <forecast.zarr>. Do not "
            "downscale the forecast to the obs grid."
        )

    pair = []
    ensemble_reduction = None
    for role, da in (("forecast", fc), ("obs", truth)):
        if getattr(getattr(da, "pint", None), "units", None) is not None:
            da = da.pint.dequantify()
        m = _member_dim(da)
        if m is not None and not (role == "forecast" and metric in _ENSEMBLE_METRICS):
            da = da.mean(m, keep_attrs=True)
            if role == "forecast":
                ensemble_reduction = f"mean over {m} before scoring"
        pair.append(da)
    fc, truth = xr.align(*pair, join="inner", exclude=[member] if member else [])
    if any(size == 0 for size in fc.sizes.values()):
        raise UsageError(
            "no overlapping coordinates between --forecast and --obs; "
            "coarsen --obs --reference-grid <forecast.zarr> and align time "
            "(step-to-time / aggregate-temporal) first."
        )

    u_fc = variable_units(forecast[fc_name])
    u_obs = variable_units(obs[obs_name])
    if (
        isinstance(u_fc, str)
        and u_fc.strip()
        and isinstance(u_obs, str)
        and u_obs.strip()
        and not units_equal(u_fc, u_obs)
    ):
        print(
            f"Warning: --forecast {fc_name!r} units={u_fc.strip()!r} and --obs "
            f"{obs_name!r} units={u_obs.strip()!r} differ. Values are compared "
            "as stored.",
            file=sys.stderr,
        )
    if metric not in ("hits", "brier") and threshold is not None:
        print(
            f"Note: --threshold {threshold} is ignored for --metric {metric}.",
            file=sys.stderr,
        )
    obs_event = None
    if metric in _ENSEMBLE_METRICS:
        valid = fc.notnull().all(member) & truth.notnull()
    else:
        valid = fc.notnull() & truth.notnull()
    if metric == "hits":
        fc_event = fc >= threshold
        obs_event = truth >= threshold
        field = xr.where(fc_event & obs_event, 1, xr.where(fc_event != obs_event, -1, 0))
    elif metric == "bias":
        field = fc - truth
    elif metric in ("mae", "rmse"):
        field = np.abs(fc - truth)
    elif metric == "crps":
        field = crps_ensemble(fc, truth, member, estimator=crps_estimator)
    else:  # brier: event = value > threshold (Sheerwater above_threshold, WeatherBench 2)
        prob = (fc > threshold).mean(member)
        field = (prob - (truth > threshold).astype(float)) ** 2
    field = field.astype("float64").where(valid)
    if reduce:
        if metric == "rmse":
            field = np.sqrt((field**2).mean(reduce, skipna=True))
        else:
            field = field.mean(reduce, skipna=True)
    field = field.astype("float32")
    field.name = _VAR[metric]

    attrs = {
        k: v
        for k, v in forecast[fc_name].attrs.items()
        if k not in ("standard_name", "GRIB_name", "GRIB_paramId")
    }
    attrs["verify_metric"] = metric
    if metric == "hits":
        attrs.update(
            long_name="Event verification",
            units="1",
            flag_values=np.array([-1, 0, 1], dtype=np.int8),
            flag_meanings="disagree below hit",
            event_threshold=threshold,
            event_variable=fc_name if fc_name == obs_name else f"{fc_name},{obs_name}",
        )
    elif metric == "bias":
        attrs["long_name"] = "Forecast bias (forecast − observation)"
    elif metric == "mae":
        attrs["long_name"] = "Mean absolute error"
    elif metric == "rmse":
        attrs["long_name"] = "Root mean square error"
    elif metric == "crps":
        attrs["long_name"] = "Continuous ranked probability score (ensemble)"
        attrs["verify_crps_estimator"] = crps_estimator
    else:
        attrs.update(long_name="Brier score", units="1", event_threshold=threshold)
    if reduce:
        attrs["verify_reduce_dims"] = ",".join(reduce)
    if ensemble_reduction:
        attrs["verify_ensemble_reduction"] = ensemble_reduction
    if member is not None and metric in _ENSEMBLE_METRICS:
        attrs["verify_ensemble_size"] = int(forecast.sizes[member])
    field.attrs = attrs

    finite = field.notnull()
    if metric == "hits":
        n_obs = int(obs_event.where(finite, False).sum())
        n_hit = int(((field == 1) & finite).sum())
        summary = (
            "hit rate n/a  (0 obs events)"
            if n_obs == 0
            else f"hit rate {100 * n_hit / n_obs:.0f}%  ({n_hit}/{n_obs} obs events)"
        )
    else:
        lat = next((n for n in ("latitude", "lat") if n in field.dims), None)
        u = format_units_for_display(u_fc or u_obs) if (u_fc or u_obs) else ""
        u_suffix = f" {u}" if u else ""
        value = None
        if lat is None:
            summary = f"{metric} n/a  (no latitude dim)"
        else:
            weights = latitude_weights(field[lat]).broadcast_like(field)
            if bool(finite.any()):
                num = (field * weights).where(finite).sum(skipna=True)
                den = weights.where(finite).sum(skipna=True)
                if den != 0 and np.isfinite(float(den)):
                    value = float(num / den)
            if value is None:
                summary = f"{metric} n/a  (no finite cells)"
            elif metric == "bias":
                sign = "+" if value >= 0 else ""
                summary = f"bias {sign}{value:.2g}{u_suffix}  (cos-lat mean)"
            elif metric == "brier":
                summary = f"Brier {value:.3g}  (cos-lat mean, event > {threshold})"
            else:
                label = {"mae": "MAE", "rmse": "RMSE", "crps": "CRPS"}[metric]
                summary = f"{label} {value:.2g}{u_suffix}  (cos-lat mean)"
    print(f"verify  {summary}")

    out = field.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


if __name__ == "__main__":
    verify()

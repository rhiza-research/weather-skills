# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
# ]
# ///
"""Score a forecast against observations: hits, bias, MAE, RMSE, ensemble CRPS or Brier score."""

import sys

import numpy as np
import pandas as pd
import pint
import pint_xarray
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_dataset import ALIASES
from weather_skills_core.standard_utils import latitude_weights
from weather_skills_core.units import dequantify_dataset, ureg, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.5"

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


def _member_dim(da):
    return next((d for d in da.dims if ALIASES.get(str(d)) == "member"), None)


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
    skill = np.abs(fc - truth).mean(member, skipna=False)
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
    pair_sum_half = (ranked * coef).sum(member, skipna=False)  # = 0.5 * sum_{i,j} |x_i - x_j|
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
    help="Verification metric: hits, bias, mae, rmse (needs --reduce), crps, or brier (needs --threshold).",
)
@weather_skill.argument(
    "--threshold",
    type=float,
    default=None,
    help="Event cutoff: hits uses >= (default 1); brier requires an explicit threshold and uses >.",
)
@weather_skill.argument(
    "--reduce",
    action="append",
    help="Dimension to average the score over (repeatable); required for rmse.",
)
@weather_skill.argument(
    "--crps-estimator",
    choices=["standard", "fair"],
    default="standard",
    help="CRPS spread denominator: M squared (standard) or M times M-1 (fair).",
)
def verify(forecast, obs, variable, metric, threshold, reduce, crps_estimator, **kwargs):
    """Score a forecast against observations: hits, bias, MAE, RMSE, ensemble CRPS or Brier score."""
    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )

    fc, truth = forecast[fc_name], obs[obs_name]
    if "step" in fc.dims and "step" not in truth.dims:
        raise UsageError(
            "forecast still has a step axis; run step-to-time before verify "
            "so valid times can align with --obs."
        )

    member = _member_dim(fc)
    if metric in _ENSEMBLE_METRICS and member is None:
        raise UsageError(f"--metric {metric} needs an ensemble forecast (a member/number dim)")
    if metric == "brier" and threshold is None:
        raise UsageError("--metric brier needs an explicit --threshold")
    if metric == "rmse" and not reduce:
        raise UsageError("--metric rmse needs --reduce DIM (e.g. time)")
    if metric == "hits" and reduce:
        raise UsageError("--reduce is not defined for --metric hits")
    if metric == "crps" and crps_estimator == "fair" and fc.sizes[member] < 2:
        raise UsageError("--crps-estimator fair needs at least 2 members")
    reduce = list(dict.fromkeys(reduce or []))
    for dim in reduce:
        if dim not in fc.dims:
            raise UsageError(f"--reduce {dim!r} is not a forecast dimension")
        if dim == member:
            raise UsageError(f"--reduce {dim!r} is the ensemble dim; handling is set by --metric")
    if metric == "hits" and threshold is None:
        threshold = 1.0
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
        m = _member_dim(da)
        if m is not None and not (role == "forecast" and metric in _ENSEMBLE_METRICS):
            # Preserve upstream's complete-member rule.
            da = da.mean(m, skipna=False, keep_attrs=True)
            if role == "forecast":
                ensemble_reduction = f"mean over {m} before scoring"
        # A scalar time (after select) becomes a length-1 dim so the inner join
        # below checks it against the other input instead of dropping it.
        if "time" in da.coords and da["time"].ndim == 0:
            da = da.expand_dims("time")
        pair.append(da)
    fc, truth = pair
    u_fc, u_obs = variable_units(fc), variable_units(truth)
    if bool(u_fc) != bool(u_obs):
        raise UsageError(
            f"--forecast {fc_name!r} units={u_fc!r} but --obs {obs_name!r} units={u_obs!r}; "
            "both inputs need units (or neither) to be compared."
        )
    if u_fc and u_obs:
        # Compare in the forecast's units: deg C against K, mm hr-1 against mm day-1.
        try:
            truth = truth.pint.to(fc.pint.units)
        except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
            raise UsageError(
                f"--forecast {fc_name!r} ({u_fc}) and --obs {obs_name!r} ({u_obs}) carry "
                "incompatible units (e.g. a total against a rate); put both on one basis "
                "with aggregate-temporal / convert-to-totals first."
            ) from None
    fc, truth = xr.align(fc, truth, join="inner")
    empty = [d for d, size in fc.sizes.items() if size == 0]
    if empty:
        spans = []
        for d in empty:
            a, b = pd.Index(pair[0][d].values), pd.Index(pair[1][d].values)
            spans.append(f"{d}: forecast {a[0]} … {a[-1]} vs obs {b[0]} … {b[-1]}")
        raise UsageError(
            f"no overlapping {', '.join(empty)} labels between --forecast and --obs "
            f"({'; '.join(spans)}). Labels must match exactly: for lat/lon, coarsen "
            "--obs --reference-grid <forecast.zarr>; for time, run step-to-time on "
            "the forecast and aggregate both with the same bins (--end-time)."
        )
    # Differences keep their units (a temperature bias is delta_degC).
    error = fc - truth
    if u_fc and u_obs:
        error = dequantify_dataset(error.to_dataset(name="error"))["error"]
    fc, truth = (da.pint.dequantify() if da.pint.units is not None else da for da in (fc, truth))
    if metric not in ("hits", "brier") and threshold is not None:
        print(
            f"Note: --threshold {threshold} is ignored for --metric {metric}.",
            file=sys.stderr,
        )

    valid = (
        fc.notnull().all(member) if metric in _ENSEMBLE_METRICS else fc.notnull()
    ) & truth.notnull()
    obs_event = None
    if metric == "hits":
        fc_event = fc >= threshold
        obs_event = truth >= threshold
        field = xr.where(fc_event & obs_event, 1, xr.where(fc_event != obs_event, -1, 0))
    elif metric == "bias":
        field = error
    elif metric in ("mae", "rmse"):
        field = np.abs(error)
    elif metric == "crps":
        field = crps_ensemble(fc, truth, member, estimator=crps_estimator)
    else:
        probability = (fc > threshold).mean(member)
        field = (probability - (truth > threshold).astype(float)) ** 2
    field = field.astype("float64").where(valid)
    if reduce:
        field = (
            np.sqrt((field**2).mean(reduce, skipna=True))
            if metric == "rmse"
            else field.mean(reduce, skipna=True)
        )
    field = field.astype("float32")
    field.name = _VAR[metric]

    attrs = {
        k: v
        for k, v in forecast[fc_name].attrs.items()
        if k not in ("standard_name", "GRIB_name", "GRIB_paramId", "short_name", "comment")
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
        attrs.update(
            long_name="Forecast bias (forecast − observation)", units=error.attrs.get("units")
        )
    elif metric == "brier":
        attrs.update(
            long_name="Brier score", units="1", event_threshold=threshold, event_comparison=">"
        )
    else:
        labels = {
            "mae": "Mean absolute error",
            "rmse": "Root mean square error",
            "crps": "Continuous ranked probability score (ensemble)",
        }
        attrs.update(long_name=labels[metric], units=error.attrs.get("units"))
    if metric == "crps":
        attrs["verify_crps_estimator"] = crps_estimator
    if reduce:
        attrs["verify_reduce_dims"] = ",".join(reduce)
    if ensemble_reduction:
        attrs["verify_ensemble_reduction"] = ensemble_reduction
    if metric in _ENSEMBLE_METRICS:
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
        score_units = field.attrs.get("units")
        u = f"{ureg.Unit(score_units):~}" if score_units and score_units != "1" else ""
        u_suffix = f" {u}" if u else ""
        value = None
        # Gridded: cos-lat weighted mean. Stations (point-value output): plain
        # mean over every station and time with both values.
        how = "cos-lat mean" if lat is not None else f"mean of {int(finite.sum())} values"
        if lat is None:
            if bool(finite.any()):
                value = float(field.where(finite).mean())
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
            summary = f"bias {sign}{value:.2g}{u_suffix}  ({how})"
        else:
            summary = f"{metric.upper()} {value:.2g}{u_suffix}  ({how})"
    print(f"verify  {summary}")

    out = field.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


if __name__ == "__main__":
    verify()

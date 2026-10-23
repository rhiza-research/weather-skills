# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
# ]
# ///
"""Score a forecast against observations cell by cell: event hits, bias, or mean absolute error."""

import sys

import numpy as np
import pandas as pd
import pint
import pint_xarray
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_utils import latitude_weights
from weather_skills_core.units import dequantify_dataset, ureg, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

_METRICS = (
    "hits",
    "bias",
    "mae",
    "brier",
    "crps",
    "crps_fair",
    "rps",
    "rmse",
    "spread",
    "contingency",
    "corr",
    "reliability",
    "roc",
)
_VAR = {
    "hits": "event_hit",
    "bias": "bias",
    "mae": "mae",
    "brier": "brier",
    "crps": "crps",
    "crps_fair": "crps_fair",
    "rps": "rps",
}
# Scored from every ensemble member; the others score the ensemble mean.
_ENSEMBLE_METRICS = ("brier", "crps", "crps_fair", "spread", "reliability", "roc")
# Pooled over time (one value per cell) or over every cell and time (a curve).
_POOLED_METRICS = ("rmse", "spread", "contingency", "corr", "reliability", "roc")
_EVENT_METRICS = ("hits", "brier", "contingency", "reliability", "roc")
_PROBABILITY_BINS = np.linspace(0.0, 1.0, 11)


def _contingency_scores(a, b, c, d):
    """POD, false alarm ratio, frequency bias and ETS from hits, false alarms, misses, rest."""
    with np.errstate(divide="ignore", invalid="ignore"):
        random_hits = (a + b) * (a + c) / (a + b + c + d)
        return {
            "pod": a / (a + c),
            "far": b / (a + b),
            "frequency_bias": (a + b) / (a + c),
            "ets": (a - random_hits) / (a + b + c - random_hits),
        }


def _pooled(metric, fc, truth, error, valid, threshold, units):
    """Metrics pooled over time per cell (rmse, spread, contingency, corr) or into curves."""
    lat = next((n for n in ("latitude", "lat") if n in truth.dims), None)
    weights = (
        latitude_weights(truth[lat]).broadcast_like(valid)
        if lat
        else xr.ones_like(valid, dtype=float)
    )
    weights = weights.where(valid, 0.0)
    total = float(weights.sum())
    if total == 0:
        raise UsageError("no cell has both a forecast and an observation to score.")

    def pooled_mean(da):
        """Cos-lat weighted mean over every valid cell and time."""
        return float((da.where(valid, 0.0) * weights).sum() / total)

    mean = fc.mean("number") if "number" in fc.dims else fc
    fc_event = (fc >= threshold).astype(float).where(fc.notnull())
    prob = fc_event.mean("number") if "number" in fc.dims else fc_event
    obs_event = (truth >= threshold).astype(float)
    data = {}
    if metric == "rmse":
        squared = (error**2).where(valid)
        data["rmse"] = (
            np.sqrt(squared.mean("time")),
            {"long_name": "Root mean square error over time", "units": units},
        )
        summary = f"RMSE {np.sqrt(pooled_mean(squared)):.2g} {units}".rstrip()
    elif metric == "spread":
        if "number" not in fc.dims:
            raise UsageError("--metric spread needs an ensemble forecast (a number dim).")
        m = fc.sizes["number"]
        # Ensemble variance with the finite-ensemble correction (m + 1) / m (Fortin et al. 2014).
        variance = (fc.var("number", ddof=1) * (m + 1) / m).where(valid)
        squared = ((mean - truth) ** 2).where(valid)
        data["spread_skill"] = (
            np.sqrt(variance.mean("time") / squared.mean("time")),
            {"long_name": "Ensemble spread / RMSE of the ensemble mean", "units": "1"},
        )
        ratio = np.sqrt(pooled_mean(variance) / pooled_mean(squared))
        summary = f"spread/skill {ratio:.2f}  (1 calibrated, < 1 overconfident)"
    elif metric == "contingency":
        f, o = mean >= threshold, truth >= threshold
        counts = [
            (x & y).where(valid, False).sum("time").astype(float)
            for x, y in ((f, o), (f, ~o), (~f, o), (~f, ~o))
        ]
        names = {
            "pod": "Probability of detection",
            "far": "False alarm ratio",
            "frequency_bias": "Frequency bias (forecast events / observed events)",
            "ets": "Equitable threat score",
        }
        for key, value in _contingency_scores(*counts).items():
            data[key] = (value, {"long_name": names[key], "units": "1"})
        a, b, c, d = (float(n.sum()) for n in counts)
        t = _contingency_scores(*(np.float64(n) for n in (a, b, c, d)))
        summary = (
            f"POD {t['pod']:.2f}  FAR {t['far']:.2f}  bias {t['frequency_bias']:.2f}  "
            f"ETS {t['ets']:.2f}  ({a:.0f} hits, {b:.0f} false alarms, {c:.0f} misses)"
        )
    elif metric == "corr":
        x, y = mean.where(valid), truth.where(valid)
        xa, ya = x - x.mean("time"), y - y.mean("time")
        r = (xa * ya).sum("time") / np.sqrt((xa**2).sum("time") * (ya**2).sum("time"))
        r = r.where(valid.sum("time") >= 3)
        data["corr"] = (r, {"long_name": "Correlation over time (forecast vs obs)", "units": "1"})
        cell_weights = latitude_weights(r[lat]).broadcast_like(r) if lat else xr.ones_like(r)
        cell_weights = cell_weights.where(r.notnull(), 0.0)
        mean_r = float((r.fillna(0.0) * cell_weights).sum() / cell_weights.sum())
        summary = f"correlation {mean_r:+.2f}  (mean over cells)"
    elif metric == "reliability":
        index = np.clip(np.floor(prob * 10 + 1e-9), 0, 9)
        rows = {"forecast_probability": [], "observed_frequency": [], "sample_fraction": []}
        for k in range(10):
            w = weights.where(index == k, 0.0)
            n = float(w.sum())
            rows["forecast_probability"].append(float((prob * w).sum()) / n if n else np.nan)
            rows["observed_frequency"].append(float((obs_event * w).sum()) / n if n else np.nan)
            rows["sample_fraction"].append(n / total)
        long = {
            "forecast_probability": "Mean forecast probability in the bin",
            "observed_frequency": "Observed event frequency in the bin",
            "sample_fraction": "Fraction of forecasts in the bin",
        }
        for key, values in rows.items():
            data[key] = (
                ("probability_bin", np.array(values)),
                {"long_name": long[key], "units": "1"},
            )
        fp, of, frac = (np.nan_to_num(np.array(rows[k])) for k in rows)
        base = pooled_mean(obs_event)
        reliability = float(np.sum(frac * (fp - of) ** 2))
        resolution = float(np.sum(frac * (of - base) ** 2))
        summary = (
            f"reliability {reliability:.3g}  resolution {resolution:.3g}  "
            f"uncertainty {base * (1 - base):.3g}  (Brier = rel - res + unc)"
        )
    else:  # roc
        levels = np.round(np.arange(0.1, 1.01, 0.1), 2)
        events, non_events = pooled_mean(obs_event), pooled_mean(1 - obs_event)
        if events == 0 or non_events == 0:
            raise UsageError("--metric roc needs both observed events and non-events.")
        hit_rate = [pooled_mean((prob >= t) * obs_event) / events for t in levels]
        false_rate = [pooled_mean((prob >= t) * (1 - obs_event)) / non_events for t in levels]
        data["hit_rate"] = (("probability_threshold", np.array(hit_rate)), {"units": "1"})
        data["false_alarm_rate"] = (("probability_threshold", np.array(false_rate)), {"units": "1"})
        # Area under the curve from (1, 1) through each threshold to (0, 0).
        x = np.array([1.0, *false_rate, 0.0])
        y = np.array([1.0, *hit_rate, 0.0])
        auc = float(np.sum((x[:-1] - x[1:]) * (y[:-1] + y[1:]) / 2))
        summary = f"ROC area {auc:.3f}  (0.5 no skill, 1 perfect)"
    print(f"verify  {summary}")

    out = xr.Dataset({key: value for key, (value, _) in data.items()})
    for key, (_, attrs) in data.items():
        out[key] = out[key].astype("float32")
        out[key].attrs = {**attrs, "verify_metric": metric}
        if metric in _EVENT_METRICS:
            out[key].attrs["event_threshold"] = threshold
    if metric == "reliability":
        out = out.assign_coords(probability_bin=(_PROBABILITY_BINS[:-1] + 0.05).round(2))
    if metric == "roc":
        out = out.assign_coords(probability_threshold=levels)
        out.attrs["roc_area"] = auc
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


def _rps(fc, truth, fc_name, obs_name):
    """Ranked probability score of quintile probabilities against observed amounts.

    The forecast holds category probabilities on a ``quintile`` dim plus the
    ``quintile_threshold`` coord (upper edge of each category, last one +inf), as
    pbc-correct writes. RPS sums the squared CDF error over the four inner edges.
    """
    if "quintile" not in fc.dims or "quintile_threshold" not in fc.coords:
        raise UsageError(
            f"--metric rps needs --forecast {fc_name!r} with a quintile dim and a "
            "quintile_threshold coord (the observed edges), as pbc-correct writes; "
            "run pbc-correct --components raw to put a raw ensemble on quintiles."
        )
    u_edge = fc["quintile_threshold"].attrs.get("units")
    if u_edge and truth.pint.units is not None:
        try:
            truth = truth.pint.to(u_edge)
        except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
            raise UsageError(
                f"--obs {obs_name!r} ({variable_units(truth)}) cannot be compared with the "
                f"forecast's quintile edges ({u_edge})."
            ) from None
    fc, truth = (da.pint.dequantify() if da.pint.units is not None else da for da in (fc, truth))
    if "time" in truth.coords and truth["time"].ndim == 0:
        truth = truth.expand_dims("time")
    fc, truth = xr.align(fc, truth, join="inner")
    empty = [d for d, size in fc.sizes.items() if size == 0]
    if empty:
        raise UsageError(
            f"no overlapping {', '.join(empty)} labels between --forecast and --obs; "
            "obs must be window means labeled at the window start (aggregate-temporal "
            "--window N --align left)."
        )
    inner = {"quintile": slice(0, -1)}
    obs_cdf = (truth <= fc["quintile_threshold"]).astype(float).isel(inner)
    cdf = fc.cumsum("quintile").isel(inner)
    valid = truth.notnull() & cdf.notnull().all("quintile")
    field = ((cdf - obs_cdf) ** 2).sum("quintile").where(valid)
    # Climatology forecasts each quintile with probability 0.2.
    clim = ((cdf["quintile"] - obs_cdf) ** 2).sum("quintile").where(valid)
    field = field.drop_vars("quintile_threshold", errors="ignore")
    field.name = "rps"
    field.attrs = {
        "long_name": "Ranked probability score (quintiles)",
        "units": "1",
        "verify_metric": "rps",
    }

    lat = next((n for n in ("latitude", "lat") if n in field.dims), None)
    weights = latitude_weights(field[lat]).broadcast_like(field) if lat else xr.ones_like(field)
    weights = weights.where(valid, 0.0)
    if float(weights.sum()) > 0:
        score = float((field * weights).sum() / weights.sum())
        ref = float((clim * weights).sum() / weights.sum())
        skill = 1 - score / ref if ref > 0 else float("nan")
        how = "cos-lat mean" if lat else "mean"
        summary = f"RPS {score:.3g}  RPSS {skill:+.3f} vs climatology  ({how})"
    else:
        summary = "rps n/a  (no finite cells)"
    print(f"verify  {summary}")

    out = field.astype("float32").to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = "rps"
    out.attrs["verify_score_summary"] = summary
    return out


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
    help="Verification metric. Per time: hits, bias, mae, brier, crps, crps_fair, rps. Over time per "
    "cell: rmse, spread, contingency, corr. Curves: reliability, roc.",
)
@weather_skill.argument(
    "--threshold",
    type=float,
    default=1.0,
    help="Event cutoff for --metric hits and brier: an event is a value >= this.",
)
def verify(forecast, obs, variable, metric, threshold, **kwargs):
    """Score a forecast against observations cell by cell: event hits, bias, or mean absolute error."""
    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )

    if metric not in _EVENT_METRICS and threshold != 1.0:
        print(
            f"Note: --threshold {threshold} is ignored for --metric {metric}.",
            file=sys.stderr,
        )

    fc, truth = forecast[fc_name], obs[obs_name]
    if "step" in fc.dims and "step" not in truth.dims:
        raise UsageError(
            "forecast still has a step axis; run step-to-time before verify "
            "so valid times can align with --obs."
        )

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

    if metric == "rps":
        return _rps(fc, truth, fc_name, obs_name)

    pair = []
    for da in (fc, truth):
        if "number" in da.dims and metric not in _ENSEMBLE_METRICS:
            # A NaN member leaves the mean NaN (no mean over the members left).
            da = da.mean("number", skipna=False, keep_attrs=True)
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

    # A NaN member leaves the cell NaN, as it does for the ensemble mean.
    valid = truth.notnull() & (fc.notnull().all("number") if "number" in fc.dims else fc.notnull())
    if metric in _POOLED_METRICS:
        return _pooled(metric, fc, truth, error, valid, threshold, error.attrs.get("units") or "")
    obs_event = None
    if metric == "hits":
        fc_event = fc >= threshold
        obs_event = truth >= threshold
        field = xr.where(fc_event & obs_event, 1, xr.where(fc_event != obs_event, -1, 0))
    elif metric == "bias":
        field = error
    elif metric == "brier":
        fc_event = fc >= threshold
        prob = fc_event.mean("number") if "number" in fc.dims else fc_event
        obs_event = truth >= threshold
        field = (prob.astype(float) - obs_event.astype(float)) ** 2
    elif metric == "crps_fair" and fc.sizes.get("number", 0) < 2:
        raise UsageError("--metric crps_fair needs an ensemble forecast with at least 2 members.")
    elif metric in ("crps", "crps_fair") and "number" in fc.dims:
        # CRPS = E|X - y| - E|X - X'| / 2, with E|X - X'| from the sorted members.
        # The fair score averages over the m(m-1) distinct member pairs, not all m^2.
        m = fc.sizes["number"]
        ranked = xr.apply_ufunc(
            np.sort, fc, input_core_dims=[["number"]], output_core_dims=[["number"]],
            kwargs={"axis": -1},
        )  # fmt: skip
        rank_weight = xr.DataArray(2.0 * np.arange(1, m + 1) - m - 1, dims="number")
        pairs = m * (m - 1) if metric == "crps_fair" else m**2
        spread = 2.0 * (ranked * rank_weight).sum("number") / pairs
        field = np.abs(fc - truth).mean("number") - 0.5 * spread
    else:
        # mae, and crps of a single forecast, which is its absolute error.
        field = np.abs(error)
    field = field.astype("float32").where(valid)
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
        attrs.update(long_name="Brier score", units="1", event_threshold=threshold)
    elif metric == "crps":
        attrs.update(
            long_name="Continuous ranked probability score", units=error.attrs.get("units")
        )
    elif metric == "crps_fair":
        attrs.update(
            long_name="Fair continuous ranked probability score", units=error.attrs.get("units")
        )
    else:
        attrs.update(long_name="Mean absolute error", units=error.attrs.get("units"))
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
        u = f"{ureg.Unit(u_fc or u_obs):~}" if (u_fc or u_obs) else ""
        u_suffix = f" {u}" if u else ""
        # Gridded: cos-lat weighted mean. Stations (point-value output): plain
        # mean over every station and time with both values.
        how = "cos-lat mean" if lat is not None else f"mean of {int(finite.sum())} values"
        weights = (
            latitude_weights(field[lat]).broadcast_like(field)
            if lat is not None
            else xr.ones_like(field)
        )
        weights = weights.where(finite, 0.0)
        total = float(weights.sum())
        value = float((field.fillna(0.0) * weights).sum() / total) if total > 0 else None
        if value is None:
            summary = f"{metric} n/a  (no finite cells)"
        elif metric == "bias":
            sign = "+" if value >= 0 else ""
            summary = f"bias {sign}{value:.2g}{u_suffix}  ({how})"
        elif metric == "brier":
            # Reference: always forecasting the observed base rate.
            rate = float((obs_event.astype(float) * weights).sum() / total)
            ref = rate * (1 - rate)
            skill = f"BSS {1 - value / ref:+.3f}" if ref > 0 else "BSS n/a (no event variation)"
            summary = f"Brier {value:.3g}  {skill} vs base rate {rate:.0%}  ({how})"
        elif metric == "crps":
            summary = f"CRPS {value:.2g}{u_suffix}  ({how})"
        elif metric == "crps_fair":
            summary = f"fair CRPS {value:.2g}{u_suffix}  ({how})"
        else:
            summary = f"MAE {value:.2g}{u_suffix}  ({how})"
    print(f"verify  {summary}")

    out = field.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


if __name__ == "__main__":
    verify()

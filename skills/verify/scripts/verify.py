# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
#   "pandas",
# ]
# ///
"""Verify in two steps: per-sample statistics from a forecast and obs, then metrics from grouped statistics."""

import json
import re
import sys
import uuid

import numpy as np
import pandas as pd
import pint
import pint_xarray
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.errors import DataError
from weather_skills_core.units import parse_aggregation_period, ureg, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.8"

# Statistics: per-sample values whose mean over any group feeds a metric.
_STATISTICS = {
    "fcst": "Forecast",
    "obs": "Observation",
    "bias": "Forecast minus observation",
    "mae": "Absolute error",
    "mse": "Squared error",
    "mape": "Absolute error relative to the observation",
    "smape": "Symmetric absolute percentage error",
    "squared_fcst": "Squared forecast",
    "squared_obs": "Squared observation",
    "covariance": "Forecast times observation",
    "squared_fcst_anom": "Squared forecast anomaly",
    "squared_obs_anom": "Squared observation anomaly",
    "anom_covariance": "Forecast anomaly times observation anomaly",
    "true_positives": "Hit (forecast and observed event)",
    "false_positives": "False alarm (forecast event, none observed)",
    "false_negatives": "Miss (observed event, none forecast)",
    "true_negatives": "Correct negative (no event forecast or observed)",
    "obs_event": "Observed event",
    "brier": "Brier score",
    "crps": "Continuous ranked probability score",
    "crps_fair": "Fair continuous ranked probability score",
    "ensemble_variance": "Ensemble variance (finite-ensemble corrected)",
    "rps": "Ranked probability score",
    "rps_clim": "Ranked probability score of climatology",
    "n_correct": "Forecast and observation in the same category",
    "n_fcst_bin": "Forecast in the category",
    "n_obs_bin": "Observation in the category",
    "reliability_count": "Forecast probability in the bin",
    "reliability_fcst": "Forecast probability, counted in its bin",
    "reliability_obs": "Observed event, counted in the forecast probability's bin",
    "roc_hits": "Observed event warned at the probability threshold",
    "roc_false_alarms": "Non-event warned at the probability threshold",
}
_THRESHOLD_STATS = {
    "true_positives",
    "false_positives",
    "false_negatives",
    "true_negatives",
    "obs_event",
    "brier",
    "reliability_count",
    "reliability_fcst",
    "reliability_obs",
    "roc_hits",
    "roc_false_alarms",
}
_PROBABILITY_STATS = {"brier", "reliability_count", "reliability_fcst", "reliability_obs"}
_PROBABILITY_STATS |= {"roc_hits", "roc_false_alarms"}
_CONTINGENCY = ("true_positives", "false_positives", "false_negatives", "true_negatives")
_ANOM_STATS = {"squared_fcst_anom", "squared_obs_anom", "anom_covariance"}
_BIN_STATS = {"n_correct", "n_fcst_bin", "n_obs_bin"}
_ROC_LEVELS = np.round(np.arange(0.1, 1.01, 0.1), 2)


# Metrics: (statistics, finalize) applied to group means of those statistics.
# A statistic whose group mean is already the metric keeps its name and units.
_IDENTITY = (
    "fcst",
    "obs",
    "bias",
    "mae",
    "mse",
    "mape",
    "smape",
    "crps",
    "crps_fair",
    "brier",
    "rps",
)
_METRICS = {name: ((name,), lambda g, name=name: {name: g[name]}) for name in _IDENTITY}
_METRICS |= {
    "rmse": (("mse",), lambda g: {"rmse": g["mse"] ** 0.5}),
    "pearson": (
        ("fcst", "obs", "squared_fcst", "squared_obs", "covariance"),
        lambda g: {
            "pearson": (g["covariance"] - g["fcst"] * g["obs"])
            / (
                (g["squared_fcst"] - g["fcst"] ** 2) ** 0.5
                * (g["squared_obs"] - g["obs"] ** 2) ** 0.5
            )
        },
    ),
    "acc": (
        ("squared_fcst_anom", "squared_obs_anom", "anom_covariance"),
        lambda g: {
            "acc": g["anom_covariance"] / (g["squared_fcst_anom"] * g["squared_obs_anom"]) ** 0.5
        },
    ),
    "bss": (
        ("brier", "obs_event"),
        lambda g: {"bss": 1 - g["brier"] / (g["obs_event"] * (1 - g["obs_event"]))},
    ),
    "pod": (
        ("true_positives", "false_negatives"),
        lambda g: {"pod": g["true_positives"] / (g["true_positives"] + g["false_negatives"])},
    ),
    "far": (
        ("true_positives", "false_positives"),
        lambda g: {"far": g["false_positives"] / (g["true_positives"] + g["false_positives"])},
    ),
    "pofd": (
        ("false_positives", "true_negatives"),
        lambda g: {"pofd": g["false_positives"] / (g["false_positives"] + g["true_negatives"])},
    ),
    "csi": (
        ("true_positives", "false_positives", "false_negatives"),
        lambda g: {
            "csi": g["true_positives"]
            / (g["true_positives"] + g["false_positives"] + g["false_negatives"])
        },
    ),
    "frequency_bias": (
        ("true_positives", "false_positives", "false_negatives"),
        lambda g: {
            "frequency_bias": (g["true_positives"] + g["false_positives"])
            / (g["true_positives"] + g["false_negatives"])
        },
    ),
    "ets": (_CONTINGENCY, lambda g: {"ets": _ets(*(g[s] for s in _CONTINGENCY))}),
    "heidke": (
        ("n_correct", "n_fcst_bin", "n_obs_bin"),
        lambda g: {"heidke": _heidke(g["n_correct"], g["n_fcst_bin"], g["n_obs_bin"])},
    ),
    "spread": (
        ("ensemble_variance", "mse"),
        lambda g: {"spread": (g["ensemble_variance"] / g["mse"]) ** 0.5},
    ),
    "rpss": (("rps", "rps_clim"), lambda g: {"rpss": 1 - g["rps"] / g["rps_clim"]}),
    "reliability": (
        ("reliability_count", "reliability_fcst", "reliability_obs"),
        lambda g: {
            "forecast_probability": g["reliability_fcst"] / g["reliability_count"],
            "observed_frequency": g["reliability_obs"] / g["reliability_count"],
            "sample_fraction": g["reliability_count"]
            / g["reliability_count"].sum("probability_bin"),
        },
    ),
    "roc": (("roc_hits", "roc_false_alarms", "obs_event"), lambda g: _roc(g)),
}
_METRIC_NAMES = {
    "rmse": "Root mean square error",
    "pearson": "Pearson correlation",
    "acc": "Anomaly correlation coefficient",
    "bss": "Brier skill score (reference: observed base rate)",
    "pod": "Probability of detection",
    "far": "False alarm ratio",
    "pofd": "Probability of false detection (false alarm rate)",
    "csi": "Critical success index",
    "frequency_bias": "Frequency bias (forecast events / observed events)",
    "ets": "Equitable threat score",
    "heidke": "Heidke skill score",
    "spread": "Ensemble spread / RMSE of the ensemble mean",
    "rpss": "Ranked probability skill score (reference: equal category odds)",
    "forecast_probability": "Mean forecast probability in the bin",
    "observed_frequency": "Observed event frequency in the bin",
    "sample_fraction": "Share of forecasts in the bin",
    "hit_rate": "Hit rate (probability of detection)",
    "false_alarm_rate": "False alarm rate (probability of false detection)",
    "roc_area": "Area under the ROC curve",
}


def _ets(a, b, c, d):
    random_hits = (a + b) * (a + c) / (a + b + c + d)
    return (a - random_hits) / (a + b + c - random_hits)


def _heidke(correct, fcst_bin, obs_bin):
    chance = (fcst_bin * obs_bin).sum("category")
    return (correct - chance) / (1 - chance)


def _roc(g):
    hit = (g["roc_hits"] / g["obs_event"]).transpose(..., "probability_threshold")
    false = (g["roc_false_alarms"] / (1 - g["obs_event"])).transpose(..., "probability_threshold")
    # Area under the curve from (1, 1) through each threshold to (0, 0), by trapezoids.
    one = np.ones((*hit.shape[:-1], 1))
    x = np.concatenate([one, false.values, 0 * one], -1)
    y = np.concatenate([one, hit.values, 0 * one], -1)
    area = ((x[..., :-1] - x[..., 1:]) * (y[..., :-1] + y[..., 1:]) / 2).sum(-1)
    return {
        "hit_rate": hit,
        "false_alarm_rate": false,
        "roc_area": hit.isel(probability_threshold=0, drop=True).copy(data=area),
    }


def _at_valid_times(da, fc, role):
    """Pair a field on ``time`` with the forecast's (init ``time``, ``step``): its value at init + step."""
    if "time" in da.coords and da["time"].ndim == 0:
        da = da.expand_dims("time")
    if "step" not in fc.dims or "step" in da.dims:
        return da
    if "time" not in fc.dims or "time" not in da.dims:
        raise UsageError(
            f"forecast has a step axis but no init time, or {role} has no time axis; "
            "cannot match leads to dates."
        )
    valid = (fc["time"] + fc["step"]).transpose("time", "step")
    pos = pd.Index(da["time"].values).get_indexer(valid.values.ravel()).reshape(valid.shape)
    if (pos < 0).all():
        raise UsageError(
            f"no {role} time matches a forecast valid time (init + step): valid "
            f"{pd.Timestamp(valid.values.min())} … {pd.Timestamp(valid.values.max())} vs {role} "
            f"{pd.Timestamp(da['time'].values.min())} … {pd.Timestamp(da['time'].values.max())}. "
            "Label both with the same bins: obs windows start on the day each forecast step starts."
        )
    attrs = da.attrs
    picked = da.drop_vars([c for c in da.coords if c != "time" and "time" in da[c].dims]).isel(
        time=xr.DataArray(np.maximum(pos, 0), dims=("init", "step"))
    )
    da = picked.drop_vars("time").rename(init="time")
    # Valid times past the record stay NaN: not scored, never filled.
    da = da.where(xr.DataArray(pos >= 0, dims=("time", "step")))
    da = da.assign_coords(time=fc["time"].values, step=fc["step"].values)
    da.attrs = attrs
    return da


def _to_units(da, units, role):
    """Plain values of ``da`` in ``units`` (no units on either side: as stored)."""
    if units is None or da.pint.units is None:
        if (units is None) != (variable_units(da) is None):
            raise UsageError(f"{role} and the forecast must both carry units (or neither).")
        return da.pint.dequantify() if da.pint.units is not None else da
    try:
        return da.pint.to(units).pint.dequantify()
    except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
        raise UsageError(
            f"{role} ({variable_units(da)}) cannot be converted to the forecast's units "
            f"({units:~}); put both on one basis (a total against a rate needs "
            "aggregate-temporal / convert-to-totals first)."
        ) from None


def _cutoff(threshold, units, attrs):
    """--threshold as a number in the forecast's units; a total spreads over a rate's window."""
    try:
        return float(threshold)
    except ValueError:
        pass
    try:
        quantity = ureg.Quantity(threshold)
    except Exception:  # noqa: BLE001 — any pint parse failure is a usage error
        raise UsageError(f"--threshold {threshold!r} is not a number or a quantity") from None
    if units is None:
        raise UsageError(f"--threshold {threshold!r} has units but the forecast has none.")
    try:
        return float(quantity.to(units).magnitude)
    except pint.DimensionalityError:
        pass
    period = attrs.get("aggregation_period") or attrs.get("data_interval")
    if not period:
        raise UsageError(
            f"--threshold {threshold!r} is a total but the forecast ({units:~}) has no "
            "aggregation_period or data_interval to spread it over."
        )
    try:
        cutoff = float((quantity / parse_aggregation_period(period)).to(units).magnitude)
    except pint.DimensionalityError:
        raise UsageError(f"--threshold {threshold!r} cannot be compared with {units:~}.") from None
    print(f"--threshold {threshold} over {period} = {cutoff:.6g} {units:~}.", file=sys.stderr)
    return cutoff


@weather_skill(
    name="verify",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--forecast", type=Dataset("any"))
@weather_skill.argument("--obs", type=Dataset("any"))
@weather_skill.argument(
    "--statistic",
    action="append",
    choices=list(_STATISTICS),
    help="Per-sample statistic to write (repeatable); needs --forecast and --obs.",
)
@weather_skill.argument(
    "--metric",
    choices=list(_METRICS),
    help="Metric from grouped statistics in --input (summarize-dim output).",
)
@weather_skill.argument("--input", "-i", type=Dataset("any"), help="Statistics Zarr, for --metric.")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--threshold",
    help=(
        "Event cutoff: an event is a value >= this. A bare number is in the forecast's units; "
        "'50 mm' on a rate is a total over its aggregation_period."
    ),
)
@weather_skill.argument(
    "--member-fraction",
    type=float,
    help="Contingency on an ensemble: the forecast event is at least this share of members.",
)
@weather_skill.argument(
    "--probability",
    action="store_true",
    help="The forecast is already the event probability (0-1, no members).",
)
@weather_skill.argument(
    "--clim", type=Dataset("any"), help="Climatology on obs dates, for the anomaly statistics."
)
@weather_skill.argument(
    "--bins",
    type=float,
    nargs="+",
    help="Category edges for n_correct / n_fcst_bin / n_obs_bin, in the forecast's units.",
)
@weather_skill.argument(
    "--fc-edges", type=Dataset("any"), help="rps: forecast category edges (a quantile dim)."
)
@weather_skill.argument(
    "--obs-edges", type=Dataset("any"), help="rps: observed category edges (a quantile dim)."
)
@weather_skill.argument(
    "--filter",
    type=Dataset("any"),
    action="append",
    help="0/1 mask (repeatable): only samples where every mask is 1 are kept.",
)
def verify(
    forecast,
    obs,
    statistic,
    metric,
    ds,
    variable,
    threshold,
    member_fraction,
    probability,
    clim,
    bins,
    fc_edges,
    obs_edges,
    filter,
    **kwargs,
):
    """Verify in two steps: per-sample statistics from a forecast and obs, then metrics from grouped statistics."""
    if metric:
        given = [
            flag
            for flag, value in (
                ("--forecast", forecast),
                ("--obs", obs),
                ("--statistic", statistic),
                ("--threshold", threshold),
                ("--member-fraction", member_fraction),
                ("--probability", probability),
                ("--clim", clim),
                ("--bins", bins),
                ("--fc-edges", fc_edges),
                ("--obs-edges", obs_edges),
                ("--filter", filter),
            )
            if value
        ]
        if given:
            raise UsageError(
                f"--metric reads grouped statistics from --input only; drop {', '.join(given)}."
            )
        if ds is None:
            raise UsageError(
                "--metric needs --input, the statistics Zarr grouped by summarize-dim."
            )
        return _metric(ds, metric, variable)
    if not statistic:
        raise UsageError("pass --statistic (with --forecast and --obs) or --metric (with --input).")
    if forecast is None or obs is None or ds is not None:
        raise UsageError("--statistic needs --forecast and --obs, and no --input.")
    stats = list(dict.fromkeys(statistic))

    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )
    fc, truth = forecast[fc_name], obs[obs_name]
    members = "number" in fc.dims
    quintiles = "quintile" in fc.dims

    def needs(flag, value, wanted):
        used = [s for s in stats if s in wanted]
        if value and not used:
            raise UsageError(f"{flag} is not used by any of: {', '.join(stats)}.")
        if used and not value:
            raise UsageError(f"{', '.join(used)} need {flag}.")

    needs("--threshold", threshold, _THRESHOLD_STATS)
    needs("--clim", clim, _ANOM_STATS)
    needs("--bins", bins, _BIN_STATS)
    if (fc_edges is None) != (obs_edges is None):
        raise UsageError("--fc-edges and --obs-edges go together.")
    if fc_edges is not None and not {"rps", "rps_clim"} & set(stats):
        raise UsageError("--fc-edges / --obs-edges are for rps and rps_clim.")
    if member_fraction is not None:
        if not set(_CONTINGENCY) & set(stats):
            raise UsageError("--member-fraction is for the contingency statistics.")
        if not 0 < member_fraction <= 1:
            raise UsageError(f"--member-fraction must be in (0, 1]; got {member_fraction}")
        if not members:
            raise UsageError("--member-fraction needs an ensemble forecast (a number dim).")
    if probability:
        if not _PROBABILITY_STATS & set(stats):
            raise UsageError("--probability is for brier, reliability_* and roc_* statistics.")
        if members:
            raise UsageError(
                "--probability takes one probability per cell, but the forecast has members "
                "(a number dim); drop --probability to score the member fraction."
            )
    for s in ("crps_fair", "ensemble_variance"):
        if s in stats and fc.sizes.get("number", 0) < 2:
            raise UsageError(f"{s} needs an ensemble forecast with at least 2 members.")
    if quintiles:
        other = [s for s in stats if s not in ("rps", "rps_clim")]
        if other or fc_edges is not None:
            raise UsageError(
                "a forecast of category probabilities (a quintile dim) only gives rps and "
                f"rps_clim, without edges; drop {', '.join(other) or '--fc-edges/--obs-edges'}."
            )
    elif {"rps", "rps_clim"} & set(stats) and (fc_edges is None or not members):
        raise UsageError(
            "rps needs either a forecast of quintile probabilities (pbc-correct) or an "
            "ensemble (number dim) with --fc-edges and --obs-edges from summarize-dim "
            "--method quantile."
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

    if "time" in fc.coords and fc["time"].ndim == 0:
        fc = fc.expand_dims("time")
    if "number" in truth.dims:
        # A NaN member leaves the mean NaN (no mean over the members left).
        truth = truth.mean("number", skipna=False, keep_attrs=True)
    truth = _at_valid_times(truth, fc, "--obs")
    units = fc.pint.units
    if quintiles:
        # Category probabilities: the units belong to the quintile_threshold edges.
        edge_units = fc["quintile_threshold"].attrs.get("units")
        units = ureg.Unit(edge_units) if edge_units else None
        fc = fc.pint.dequantify() if fc.pint.units is not None else fc
    elif units is not None:
        if ureg.Quantity(0.0, units).to_base_units().magnitude != 0:
            # Squares and products of °C are meaningless; score offset units as kelvin.
            print(f"verify: scoring in K, not {units:~}.", file=sys.stderr)
            units = ureg.Unit("K")
        fc = fc.pint.to(units).pint.dequantify()
    truth = _to_units(truth, units, f"--obs {obs_name!r}")

    extras = {}
    if clim is not None:
        name = next(
            (n for n in (variable, f"{fc_name}_avg", fc_name, f"{obs_name}_avg") if n in clim),
            auto_variable(clim),
        )
        extras["clim"] = _to_units(_at_valid_times(clim[name], fc, "--clim"), units, "--clim")
    for i, eds in enumerate((fc_edges, obs_edges) if fc_edges is not None else ()):
        role = ("--fc-edges", "--obs-edges")[i]
        name = next((n for n in (variable, fc_name, obs_name) if n in eds), auto_variable(eds))
        e = eds[name]
        if "quantile" not in e.dims or "number" in e.dims:
            raise UsageError(
                f"{role} {name!r} has dims {list(e.dims)}; it needs a quantile dim and no "
                "number dim (summarize-dim --dim time --dim number --method quantile --q 0.333 0.667)."
            )
        e = _to_units(e.sortby("quantile"), units, role)
        extras[role] = e.drop_vars("quantile").rename(quantile="category_edge")
    if fc_edges is not None and (
        extras["--fc-edges"].sizes["category_edge"] != extras["--obs-edges"].sizes["category_edge"]
    ):
        raise UsageError("--fc-edges and --obs-edges must hold the same number of quantiles.")
    for i, mds in enumerate(filter or ()):
        m = mds[auto_variable(mds) or next(iter(mds.data_vars))]
        if "number" in m.dims:
            raise UsageError(f"--filter #{i + 1} has a number dim; a mask applies to every member.")
        m = _at_valid_times(m, fc, f"--filter #{i + 1}")
        m = m.pint.dequantify() if m.pint.units is not None else m
        # Missing mask values leave the sample out, as 0 does.
        extras[f"filter{i}"] = m.fillna(0) != 0

    names = list(extras)
    fc, truth, *rest = xr.align(fc, truth, *extras.values(), join="inner")
    extras = dict(zip(names, rest, strict=True))
    empty = [d for d, size in fc.sizes.items() if size == 0]
    if empty:
        raise UsageError(
            f"no overlapping {', '.join(empty)} labels between --forecast, --obs and the other "
            "inputs. Labels must match exactly: for lat/lon, coarsen --obs --reference-grid "
            "<forecast.zarr>; for time, aggregate both with the same bins."
        )

    cutoff = _cutoff(threshold, units, fc.attrs) if threshold else None
    if probability:
        lo, hi = float(fc.min(skipna=True)), float(fc.max(skipna=True))
        if lo < 0 or hi > 1:
            raise DataError(
                f"--probability needs forecast values in 0-1; {fc_name!r} spans {lo:g} to {hi:g}."
            )
    # A NaN member leaves the sample out, as it leaves the ensemble mean NaN.
    core = [d for d in ("number", "quintile") if d in fc.dims]
    valid = (fc.notnull().all(core) if core else fc.notnull()) & truth.notnull()
    for key, e in extras.items():
        if key.startswith("filter"):
            valid = valid & e
        elif key == "clim":
            valid = valid & e.notnull()
        else:
            valid = valid & e.notnull().all("category_edge")
    mean = fc.mean("number") if members else fc
    error = mean - truth
    unit_sq = units**2 if units is not None else None
    if cutoff is not None:
        obs_event = (truth >= cutoff).astype(float)
        if probability:
            prob = fc
        else:
            prob = (fc >= cutoff).astype(float)
            prob = prob.mean("number") if members else prob
        if member_fraction is not None:
            fc_event = prob >= member_fraction
        else:
            fc_event = mean >= cutoff

    out = {}
    for s in stats:
        u = None
        if s == "fcst":
            field, u = mean, units
        elif s == "obs":
            field, u = truth, units
        elif s == "bias":
            field, u = error, units
        elif s == "mae":
            field, u = np.abs(error), units
        elif s == "mse":
            field, u = error**2, unit_sq
        elif s == "mape":
            field = np.abs(error) / np.maximum(np.abs(truth), 1e-10)
        elif s == "smape":
            total = np.abs(mean) + np.abs(truth)
            # Both zero is a perfect forecast, not 0/0.
            field = xr.where(total > 0, np.abs(error) / total.where(total > 0), 0.0)
        elif s == "squared_fcst":
            field, u = mean**2, unit_sq
        elif s == "squared_obs":
            field, u = truth**2, unit_sq
        elif s == "covariance":
            field, u = mean * truth, unit_sq
        elif s == "squared_fcst_anom":
            field, u = (mean - extras["clim"]) ** 2, unit_sq
        elif s == "squared_obs_anom":
            field, u = (truth - extras["clim"]) ** 2, unit_sq
        elif s == "anom_covariance":
            field, u = (mean - extras["clim"]) * (truth - extras["clim"]), unit_sq
        elif s == "true_positives":
            field = (fc_event & (obs_event == 1)).astype(float)
        elif s == "false_positives":
            field = (fc_event & (obs_event == 0)).astype(float)
        elif s == "false_negatives":
            field = (~fc_event & (obs_event == 1)).astype(float)
        elif s == "true_negatives":
            field = (~fc_event & (obs_event == 0)).astype(float)
        elif s == "obs_event":
            field = obs_event
        elif s == "brier":
            field = (prob - obs_event) ** 2
        elif s in ("crps", "crps_fair"):
            u = units
            if members:
                # CRPS = E|X - y| - E|X - X'| / 2, with E|X - X'| from the sorted members.
                # The fair score averages over the m(m-1) distinct member pairs, not all m^2.
                m = fc.sizes["number"]
                ranked = xr.apply_ufunc(
                    np.sort, fc, input_core_dims=[["number"]], output_core_dims=[["number"]],
                    kwargs={"axis": -1},
                )  # fmt: skip
                rank_weight = xr.DataArray(2.0 * np.arange(1, m + 1) - m - 1, dims="number")
                pairs = m * (m - 1) if s == "crps_fair" else m**2
                spread = 2.0 * (ranked * rank_weight).sum("number") / pairs
                field = np.abs(fc - truth).mean("number") - 0.5 * spread
            else:
                field = np.abs(error)
        elif s == "ensemble_variance":
            m = fc.sizes["number"]
            # Finite-ensemble correction (m + 1) / m (Fortin et al. 2014).
            field, u = fc.var("number", ddof=1) * (m + 1) / m, unit_sq
        elif s in ("rps", "rps_clim") and quintiles:
            inner = {"quintile": slice(0, -1)}
            obs_cdf = (truth <= fc["quintile_threshold"]).astype(float).isel(inner)
            cdf = fc["quintile"] if s == "rps_clim" else fc.cumsum("quintile")
            field = ((cdf.isel(inner) - obs_cdf) ** 2).sum("quintile")
            field = field.drop_vars("quintile_threshold", errors="ignore")
        elif s in ("rps", "rps_clim"):
            n_cat = extras["--fc-edges"].sizes["category_edge"] + 1
            obs_cdf = (truth <= extras["--obs-edges"]).astype(float)
            if s == "rps":
                cdf = (fc <= extras["--fc-edges"]).mean("number")
            else:
                cdf = xr.DataArray(np.arange(1, n_cat) / n_cat, dims="category_edge")
            field = ((cdf - obs_cdf) ** 2).sum("category_edge")
        elif s in _BIN_STATS:
            edges = np.sort(np.asarray(bins, dtype=float))
            category = xr.DataArray(np.arange(edges.size + 1), dims="category")
            # Category k holds values from edge k-1 (inclusive) up to edge k.
            f_cat = xr.apply_ufunc(np.digitize, mean, kwargs={"bins": edges})
            o_cat = xr.apply_ufunc(np.digitize, truth, kwargs={"bins": edges})
            if s == "n_correct":
                field = (f_cat == o_cat).astype(float)
            else:
                field = ((f_cat if s == "n_fcst_bin" else o_cat) == category).astype(float)
                field = field.assign_coords(category=category)
        elif s.startswith("reliability_"):
            centre = np.round(np.arange(0.05, 1.0, 0.1), 2)
            index = np.clip(np.floor(prob * 10 + 1e-9), 0, 9)
            in_bin = (index == xr.DataArray(np.arange(10), dims="probability_bin")).astype(float)
            weight = {"count": 1.0, "fcst": prob, "obs": obs_event}[s.removeprefix("reliability_")]
            field = (in_bin * weight).assign_coords(probability_bin=centre)
        else:  # roc_hits, roc_false_alarms
            level = xr.DataArray(_ROC_LEVELS, dims="probability_threshold")
            event = obs_event if s == "roc_hits" else 1 - obs_event
            field = ((prob >= level) * event).assign_coords(probability_threshold=_ROC_LEVELS)
        out[s] = (field, u)

    # One sample set for every statistic: a NaN in any statistic drops that sample from all,
    # so their group means pool the same samples.
    for field, _ in out.values():
        extra = [d for d in field.dims if d not in valid.dims]
        valid = valid & (field.notnull().all(extra) if extra else field.notnull())
    out["n_valid"] = (xr.ones_like(valid, dtype=float), None)
    n = int(valid.sum())
    if n == 0:
        raise DataError("no sample has both a forecast and an observation (after any --filter).")

    config = {
        "forecast_variable": fc_name,
        "obs_variable": obs_name,
        "threshold": cutoff,
        "member_fraction": member_fraction,
        "probability": bool(probability),
        "clim": clim is not None,
        "bins": list(bins) if bins else None,
        "edges": fc_edges is not None,
        "filters": len(filter or ()),
    }
    run = uuid.uuid4().hex
    base = {
        k: v
        for k, v in forecast[fc_name].attrs.items()
        if k
        not in ("standard_name", "GRIB_name", "GRIB_paramId", "short_name", "comment", "long_name")
    }
    result = xr.Dataset()
    for s, (field, u) in out.items():
        field = field.where(valid).astype("float32")
        field.attrs = {
            **base,
            "long_name": _STATISTICS.get(s, "Sample counted (1) in the statistics"),
            "verify_statistic": s,
            "verify_run": run,
            "verify_config": json.dumps(config),
            "verify_cell_methods": base.get("cell_methods", ""),
        }
        field.attrs.pop("units", None)
        result[s] = field.pint.quantify("1" if u is None else u)
    result.attrs["Conventions"] = "CF-1.13"
    result.attrs["verify_statistics"] = " ".join(out)
    print(f"verify  {len(stats)} statistic(s) on {n} samples: {', '.join(stats)}")
    return result


def _metric(ds, metric, variable):
    """Finalize a metric from group means of its statistics, refusing statistics it cannot use."""
    needed, finalize = _METRICS[metric]
    missing = [s for s in needed if s not in ds or ds[s].attrs.get("verify_statistic") != s]
    if missing:
        flags = " ".join(f"--statistic {s}" for s in needed)
        raise UsageError(
            f"--metric {metric} needs the statistics {', '.join(needed)}; missing "
            f"{', '.join(missing)}. Write them in one call (verify {flags} --forecast … --obs …), "
            "group with summarize-dim --method mean, then rerun."
        )
    if len({ds[s].attrs.get("verify_run") for s in needed}) > 1:
        raise UsageError(
            f"--metric {metric}: {', '.join(needed)} come from different verify calls, so they "
            "may pool different samples; write them in one verify call."
        )
    added = {}
    for s in needed:
        cm, start = ds[s].attrs.get("cell_methods", ""), ds[s].attrs.get("verify_cell_methods", "")
        if not cm.startswith(start):
            raise UsageError(f"--metric {metric}: {s!r} cell_methods changed after verify: {cm!r}")
        added[s] = cm[len(start) :].strip()
    if len(set(added.values())) > 1:
        detail = "; ".join(f"{s}: {v or 'not reduced'}" for s, v in added.items())
        raise UsageError(
            f"--metric {metric}: its statistics were reduced differently ({detail}); "
            "group them together in one summarize-dim call."
        )
    steps = next(iter(added.values()))
    if not steps:
        raise UsageError(
            f"--metric {metric}: {', '.join(needed)} were never grouped. Average them first "
            "(summarize-dim --dim time or --by LABEL --method mean), then rerun."
        )
    tokens = re.sub(r"\([^)]*\)", " ", steps).split()
    bad = sorted({t for t in tokens if not t.endswith(":") and t != "mean"})
    if bad:
        raise UsageError(
            f"--metric {metric} needs group means; the statistics were reduced with "
            f"{', '.join(bad)} ({steps!r}). Regroup with summarize-dim --method mean."
        )
    if variable:
        print("Note: --variable is ignored with --metric.", file=sys.stderr)

    first = ds[needed[0]].attrs
    plain = {s: ds[s].pint.dequantify() if ds[s].pint.units is not None else ds[s] for s in needed}
    units_in = plain[needed[0]].attrs.get("units")
    with np.errstate(divide="ignore", invalid="ignore"):
        results = finalize(plain)
    out = xr.Dataset()
    for name, field in results.items():
        field = field.astype("float32")
        values = np.asarray(field.values)
        if metric in _IDENTITY and units_in:
            field = field.pint.quantify(units_in)
        elif metric == "rmse" and units_in:
            # finalize took the root of the values; take it of the units too.
            root = (xr.DataArray(1.0).pint.quantify(units_in) ** 0.5).pint.units
            field = field.pint.quantify(root)
        else:
            field = field.pint.quantify("1")
        field.attrs = {
            "long_name": _METRIC_NAMES.get(name, first.get("long_name", name)),
            "cell_methods": first.get("cell_methods", ""),
            "verify_metric": metric,
            "verify_config": first.get("verify_config", ""),
        }
        out[name] = field
        u = f" {field.pint.units:~}" if field.pint.units != ureg.dimensionless else ""
        if values.size == 1:
            print(f"verify  {name} {float(values.ravel()[0]):.4g}{u}")
        else:
            finite = values[np.isfinite(values)]
            span = f"{finite.min():.4g} … {finite.max():.4g}{u}" if finite.size else "all NaN"
            print(f"verify  {name} on {dict(field.sizes)}: {span}")
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    return out


if __name__ == "__main__":
    verify()

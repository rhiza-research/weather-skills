# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.4.0",
#   "pandas",
# ]
# ///
"""Verify a forecast against obs: per-sample statistics, pooled over groups, into metrics.

--forecast ─┐   statistics per sample      group means over the dims   metrics
            ├─► (mse, fcst, obs, …)    ─►  not kept, per --by label ─► rmse = √mean(mse), csi, …
--obs ──────┘
"""

import json
import sys
from collections import namedtuple
from functools import partial

import numpy as np
import pandas as pd
import pint
import pint_xarray
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.errors import DataError
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    ureg,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.12"


# Statistic functions: each takes the paired data (p) and returns one value per sample.
# p holds fc (members kept), mean (ensemble mean), truth, error (mean - truth), clim, the
# rps edges, bins, prob (forecast event probability), obs_event and fc_event.
def _fcst(p):
    return p["mean"]


def _obs(p):
    return p["truth"]


def _bias(p):
    return p["error"]


def _mae(p):
    return np.abs(p["error"])


def _mse(p):
    return p["error"] ** 2


def _mape(p):
    return np.abs(p["error"]) / np.maximum(np.abs(p["truth"]), 1e-10)


def _smape(p):
    total = np.abs(p["mean"]) + np.abs(p["truth"])
    # Both zero is a perfect forecast, not 0/0.
    return xr.where(total > 0, np.abs(p["error"]) / total.where(total > 0), 0.0)


def _squared_fcst(p):
    return p["mean"] ** 2


def _squared_obs(p):
    return p["truth"] ** 2


def _covariance(p):
    return p["mean"] * p["truth"]


def _squared_fcst_anom(p):
    return (p["mean"] - p["clim"]) ** 2


def _squared_obs_anom(p):
    return (p["truth"] - p["clim"]) ** 2


def _anom_covariance(p):
    return (p["mean"] - p["clim"]) * (p["truth"] - p["clim"])


def _contingency(p, fc_event, obs_event):
    return ((p["fc_event"] == fc_event) & (p["obs_event"] == obs_event)).astype(float)


def _obs_event(p):
    return p["obs_event"]


def _brier(p):
    return (p["prob"] - p["obs_event"]) ** 2


def _crps(p, fair):
    fc = p["fc"]
    if "number" not in fc.dims:
        return np.abs(p["error"])
    # CRPS = E|X - y| - E|X - X'| / 2, with E|X - X'| from the sorted members.
    # The fair score averages over the m(m-1) distinct member pairs, not all m^2.
    m = fc.sizes["number"]
    ranked = xr.apply_ufunc(
        np.sort, fc, input_core_dims=[["number"]], output_core_dims=[["number"]],
        kwargs={"axis": -1},
    )  # fmt: skip
    rank_weight = xr.DataArray(2.0 * np.arange(1, m + 1) - m - 1, dims="number")
    spread = 2.0 * (ranked * rank_weight).sum("number") / (m * (m - 1) if fair else m**2)
    return np.abs(fc - p["truth"]).mean("number") - 0.5 * spread


def _ensemble_variance(p):
    m = p["fc"].sizes["number"]
    # Finite-ensemble correction (m + 1) / m (Fortin et al. 2014).
    return p["fc"].var("number", ddof=1) * (m + 1) / m


def _rps(p, clim):
    # A value on an edge is in the upper category, as an event is a value >= threshold.
    fc = p["fc"]
    if "quintile" in fc.dims:
        inner = {"quintile": slice(0, -1)}
        obs_cdf = (p["truth"] < fc["quintile_threshold"]).astype(float).isel(inner)
        cdf = fc["quintile"] if clim else fc.cumsum("quintile")
        field = ((cdf.isel(inner) - obs_cdf) ** 2).sum("quintile")
        return field.drop_vars("quintile_threshold", errors="ignore")
    obs_cdf = (p["truth"] < p["obs_edges"]).astype(float)
    if clim:
        n_cat = p["fc_edges"].sizes["category_edge"] + 1
        cdf = xr.DataArray(np.arange(1, n_cat) / n_cat, dims="category_edge")
    else:
        cdf = (fc < p["fc_edges"]).mean("number")
    return ((cdf - obs_cdf) ** 2).sum("category_edge")


def _categories(p, which):
    """1 where the forecast and obs fall in the same category (which="both"), or a 0/1
    indicator of the forecast's or obs's category on a category dim."""
    edges = np.sort(np.asarray(p["bins"], dtype=float))
    # Category k holds values from edge k-1 (inclusive) up to edge k.
    f_cat = xr.apply_ufunc(np.digitize, p["mean"], kwargs={"bins": edges})
    o_cat = xr.apply_ufunc(np.digitize, p["truth"], kwargs={"bins": edges})
    if which == "both":
        return (f_cat == o_cat).astype(float)
    category = xr.DataArray(np.arange(edges.size + 1), dims="category")
    field = ((f_cat if which == "fcst" else o_cat) == category).astype(float)
    return field.assign_coords(category=category)


def _reliability_bin(p, weight):
    """The weight (1, the forecast probability, or the observed event) in the forecast
    probability's bin of 0.1."""
    centre = np.round(np.arange(0.05, 1.0, 0.1), 2)
    # float32 probabilities read back a hair low (0.7 as 0.6999999); 1e-6 absorbs it.
    index = np.clip(np.floor(p["prob"] * 10 + 1e-6), 0, 9)
    in_bin = (index == xr.DataArray(np.arange(10), dims="probability_bin")).astype(float)
    value = {"count": 1.0, "fcst": p["prob"], "obs": p["obs_event"]}[weight]
    return (in_bin * value).assign_coords(probability_bin=centre)


def _roc_warned(p, event):
    """Samples with the observed event (event=1) or without it (0), warned at each level."""
    levels = np.round(np.arange(0.1, 1.01, 0.1), 2)
    level = xr.DataArray(levels, dims="probability_threshold")
    observed = p["obs_event"] if event else 1 - p["obs_event"]
    return ((p["prob"] >= level - 1e-6) * observed).assign_coords(probability_threshold=levels)


# Statistics: per-sample values whose mean over any group feeds a metric.
_STATISTICS = {
    "fcst": _fcst,
    "obs": _obs,
    "bias": _bias,
    "mae": _mae,
    "mse": _mse,
    "mape": _mape,
    "smape": _smape,
    "squared_fcst": _squared_fcst,
    "squared_obs": _squared_obs,
    "covariance": _covariance,
    "squared_fcst_anom": _squared_fcst_anom,
    "squared_obs_anom": _squared_obs_anom,
    "anom_covariance": _anom_covariance,
    "true_positives": partial(_contingency, fc_event=True, obs_event=1),
    "false_positives": partial(_contingency, fc_event=True, obs_event=0),
    "false_negatives": partial(_contingency, fc_event=False, obs_event=1),
    "true_negatives": partial(_contingency, fc_event=False, obs_event=0),
    "obs_event": _obs_event,
    "brier": _brier,
    "crps": partial(_crps, fair=False),
    "crps_fair": partial(_crps, fair=True),
    "ensemble_variance": _ensemble_variance,
    "rps": partial(_rps, clim=False),
    "rps_clim": partial(_rps, clim=True),
    "n_correct": partial(_categories, which="both"),
    "n_fcst_bin": partial(_categories, which="fcst"),
    "n_obs_bin": partial(_categories, which="obs"),
    "reliability_count": partial(_reliability_bin, weight="count"),
    "reliability_fcst": partial(_reliability_bin, weight="fcst"),
    "reliability_obs": partial(_reliability_bin, weight="obs"),
    "roc_hits": partial(_roc_warned, event=1),
    "roc_false_alarms": partial(_roc_warned, event=0),
}
_CONTINGENCY = ("true_positives", "false_positives", "false_negatives", "true_negatives")


# Metric functions: each takes the group means of its statistics (g) and returns the metric,
# or a dict of named outputs.
def _identity(g):
    """The group mean of the metric's one statistic is the metric."""
    (value,) = g.values()
    return value


def _rmse(g):
    return g["mse"] ** 0.5


def _pearson(g):
    fcst_sd = (g["squared_fcst"] - g["fcst"] ** 2) ** 0.5
    obs_sd = (g["squared_obs"] - g["obs"] ** 2) ** 0.5
    return (g["covariance"] - g["fcst"] * g["obs"]) / (fcst_sd * obs_sd)


def _acc(g):
    return g["anom_covariance"] / (g["squared_fcst_anom"] * g["squared_obs_anom"]) ** 0.5


def _bss(g):
    return 1 - g["brier"] / (g["obs_event"] * (1 - g["obs_event"]))


def _pod(g):
    return g["true_positives"] / (g["true_positives"] + g["false_negatives"])


def _far(g):
    return g["false_positives"] / (g["true_positives"] + g["false_positives"])


def _pofd(g):
    return g["false_positives"] / (g["false_positives"] + g["true_negatives"])


def _csi(g):
    return g["true_positives"] / (g["true_positives"] + g["false_positives"] + g["false_negatives"])


def _balanced_error_rate(g):
    return (1 - _pod(g) + _pofd(g)) / 2  # mean of the miss rate and pofd


def _frequency_bias(g):
    return (g["true_positives"] + g["false_positives"]) / (
        g["true_positives"] + g["false_negatives"]
    )


def _ets(g):
    a, b, c, d = (g[s] for s in _CONTINGENCY)
    random_hits = (a + b) * (a + c) / (a + b + c + d)
    return (a - random_hits) / (a + b + c - random_hits)


def _heidke(g):
    chance = (g["n_fcst_bin"] * g["n_obs_bin"]).sum("category")
    return (g["n_correct"] - chance) / (1 - chance)


def _spread(g):
    return (g["ensemble_variance"] / g["mse"]) ** 0.5


def _rpss(g):
    return 1 - g["rps"] / g["rps_clim"]


def _reliability(g):
    count = g["reliability_count"]
    return {
        "forecast_probability": g["reliability_fcst"] / count,
        "observed_frequency": g["reliability_obs"] / count,
        "sample_fraction": count / count.sum("probability_bin"),
    }


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


# A metric: its statistics, its function, the long name of each output, the inputs it needs
# and the optional inputs it takes ("ensemble" = a forecast with >= 2 members), and the units
# of its outputs ("units" of the forecast, "units^2", or None for unitless).
Metric = namedtuple(
    "Metric", "statistics function names needs allows units", defaults=(set(), set(), None)
)
_EVENT = {"needs": {"--threshold"}, "allows": {"--member-fraction"}}
_PROBABILITY = {"needs": {"--threshold"}, "allows": {"--probability"}}
_EDGES = {"allows": {"--fc-edges", "--obs-edges"}}
_METRICS = {
    "fcst": Metric(("fcst",), _identity, {"fcst": "Mean forecast"}, units="units"),
    "obs": Metric(("obs",), _identity, {"obs": "Mean observation"}, units="units"),
    "bias": Metric(("bias",), _identity, {"bias": "Mean forecast - observation"}, units="units"),
    "mae": Metric(("mae",), _identity, {"mae": "Mean absolute error"}, units="units"),
    "mse": Metric(("mse",), _identity, {"mse": "Mean square error"}, units="units^2"),
    "rmse": Metric(("mse",), _rmse, {"rmse": "Root mean square error"}, units="units"),
    "mape": Metric(("mape",), _identity, {"mape": "Mean absolute percentage error"}),
    "smape": Metric(("smape",), _identity, {"smape": "Symmetric mean absolute percentage error"}),
    "crps": Metric(
        ("crps",), _identity, {"crps": "Continuous ranked probability score"}, units="units"
    ),
    "crps_fair": Metric(
        ("crps_fair",),
        _identity,
        {"crps_fair": "Fair continuous ranked probability score"},
        needs={"ensemble"},
        units="units",
    ),
    "spread": Metric(
        ("ensemble_variance", "mse"),
        _spread,
        {"spread": "Ensemble spread / RMSE of the ensemble mean"},
        needs={"ensemble"},
    ),
    "pearson": Metric(
        ("fcst", "obs", "squared_fcst", "squared_obs", "covariance"),
        _pearson,
        {"pearson": "Pearson correlation"},
    ),
    "acc": Metric(
        ("squared_fcst_anom", "squared_obs_anom", "anom_covariance"),
        _acc,
        {"acc": "Anomaly correlation coefficient"},
        needs={"--clim"},
    ),
    "pod": Metric(
        ("true_positives", "false_negatives"),
        _pod,
        {"pod": "Probability of detection"},
        **_EVENT,
    ),
    "far": Metric(
        ("true_positives", "false_positives"), _far, {"far": "False alarm ratio"}, **_EVENT
    ),
    "pofd": Metric(
        ("false_positives", "true_negatives"),
        _pofd,
        {"pofd": "Probability of false detection (false alarm rate)"},
        **_EVENT,
    ),
    "csi": Metric(_CONTINGENCY[:3], _csi, {"csi": "Critical success index"}, **_EVENT),
    "balanced_error_rate": Metric(
        _CONTINGENCY,
        _balanced_error_rate,
        {"balanced_error_rate": "Balanced error rate (mean of miss rate and false alarm rate)"},
        **_EVENT,
    ),
    "frequency_bias": Metric(
        _CONTINGENCY[:3],
        _frequency_bias,
        {"frequency_bias": "Frequency bias (forecast events / observed events)"},
        **_EVENT,
    ),
    "ets": Metric(_CONTINGENCY, _ets, {"ets": "Equitable threat score"}, **_EVENT),
    "heidke": Metric(
        ("n_correct", "n_fcst_bin", "n_obs_bin"),
        _heidke,
        {"heidke": "Heidke skill score"},
        needs={"--bins"},
    ),
    "brier": Metric(("brier",), _identity, {"brier": "Brier score"}, **_PROBABILITY),
    "bss": Metric(
        ("brier", "obs_event"),
        _bss,
        {"bss": "Brier skill score (reference: observed base rate)"},
        **_PROBABILITY,
    ),
    "reliability": Metric(
        ("reliability_count", "reliability_fcst", "reliability_obs"),
        _reliability,
        {
            "forecast_probability": "Mean forecast probability in the bin",
            "observed_frequency": "Observed event frequency in the bin",
            "sample_fraction": "Share of forecasts in the bin",
        },
        **_PROBABILITY,
    ),
    "roc": Metric(
        ("roc_hits", "roc_false_alarms", "obs_event"),
        _roc,
        {
            "hit_rate": "Hit rate (probability of detection)",
            "false_alarm_rate": "False alarm rate (probability of false detection)",
            "roc_area": "Area under the ROC curve",
        },
        **_PROBABILITY,
    ),
    "rps": Metric(("rps",), _identity, {"rps": "Ranked probability score"}, **_EDGES),
    "rpss": Metric(
        ("rps", "rps_clim"),
        _rpss,
        {"rpss": "Ranked probability skill score (reference: equal category odds)"},
        **_EDGES,
    ),
}


def _at_valid_times(da, fc, role):
    """Pair a field on ``time`` with the forecast's (init ``time``, ``step``): its value at init + step."""
    if "time" in da.coords and da["time"].ndim == 0:
        da = da.expand_dims("time")
    if "step" not in fc.dims or "step" in da.dims or "time" not in da.dims:
        # No time axis: a static field (a land mask, a fixed climatology) holds for every lead.
        return da
    if "time" not in fc.dims:
        raise UsageError(
            f"forecast has a step axis but no init time; cannot match {role} dates to leads."
        )
    init, lead = fc["time"].values, fc["step"].values
    if init.dtype == object:
        # cftime dates (noleap, 360_day) add Python timedeltas, not timedelta64.
        lead = np.array(pd.to_timedelta(lead).to_pytimedelta())
    valid = init[:, None] + lead[None, :]
    pos = pd.Index(da["time"].values).get_indexer(valid.ravel()).reshape(valid.shape)
    if (pos < 0).all():
        raise UsageError(
            f"no {role} time matches a forecast valid time (init + step): valid "
            f"{valid.min()} … {valid.max()} vs {role} "
            f"{da['time'].values.min()} … {da['time'].values.max()}. "
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
        return da.pint.dequantify()
    try:
        return da.pint.to(units).pint.dequantify()
    except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
        raise UsageError(
            f"{role} ({variable_units(da)}) cannot be converted to the forecast's units "
            f"({units:~}); put both on one basis: compare rates (mm day-1 means from "
            "aggregate-temporal on both sides), or totals from convert-to-totals on both sides."
        ) from None


@weather_skill(
    name="verify",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--forecast", type=Dataset("any"))
@weather_skill.argument("--obs", type=Dataset("any"))
@weather_skill.argument(
    "--metric",
    action="append",
    choices=list(_METRICS),
    required=True,
    help="Metric (repeatable); all go into one output Zarr.",
)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--threshold",
    type=float,
    help=(
        "Event cutoff, in the forecast's units: an event is a value >= this "
        "(10 on mm day-1 data means 10 mm per day)."
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
@weather_skill.argument("--clim", type=Dataset("any"), help="Climatology on obs dates, for acc.")
@weather_skill.argument(
    "--bins",
    type=float,
    nargs="+",
    help="Category edges for heidke, in the forecast's units.",
)
@weather_skill.argument(
    "--fc-edges", type=Dataset("any"), help="rps/rpss: forecast category edges (a quantile dim)."
)
@weather_skill.argument(
    "--obs-edges", type=Dataset("any"), help="rps/rpss: observed category edges (a quantile dim)."
)
@weather_skill.argument(
    "--filter",
    type=Dataset("any"),
    action="append",
    help="0/1 mask (repeatable): only samples where every mask is 1 are kept.",
)
@weather_skill.argument(
    "--keep",
    action="append",
    help="Dim to keep (repeatable), e.g. step for one value per lead; every other dim is pooled.",
)
@weather_skill.argument(
    "--by",
    action="append",
    help="Label coord (label-group) to pool samples by (repeatable): one value per group.",
)
@weather_skill.argument(
    "--lat-weighted",
    action="store_true",
    help="Weigh each grid cell by cos(latitude) when pooling.",
)
def verify(
    forecast,
    obs,
    metric,
    variable,
    threshold,
    member_fraction,
    probability,
    clim,
    bins,
    fc_edges,
    obs_edges,
    filter,
    keep,
    by,
    lat_weighted,
    **kwargs,
):
    """Verify a forecast against obs: per-sample statistics, pooled over groups, into metrics."""
    metrics = list(dict.fromkeys(metric))
    flags = {
        "--forecast": forecast,
        "--obs": obs,
        "--threshold": threshold,
        "--member-fraction": member_fraction,
        "--probability": probability or None,
        "--clim": clim,
        "--bins": bins,
        "--fc-edges": fc_edges,
        "--obs-edges": obs_edges,
        "--filter": filter,
    }
    given = {flag for flag, value in flags.items() if value is not None}
    if forecast is None or obs is None:
        raise UsageError("verify needs --forecast and --obs.")
    # Every statistic the metrics need, each once.
    statistics = list(dict.fromkeys(c for m in metrics for c in _METRICS[m].statistics))

    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, data, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in data:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(data.data_vars)}"
            )
    fc, truth = forecast[fc_name], obs[obs_name]
    members = "number" in fc.dims
    quintiles = "quintile" in fc.dims

    # Each metric's inputs: every one it needs is given, and every input given is used.
    have = given | ({"ensemble"} if fc.sizes.get("number", 0) >= 2 else set())
    for m in metrics:
        for need in sorted(_METRICS[m].needs - have):
            what = "an ensemble forecast with at least 2 members" if need == "ensemble" else need
            raise UsageError(f"--metric {m} needs {what}.")
    for flag in sorted(given - {"--forecast", "--obs", "--filter"}):
        if not any(flag in _METRICS[m].needs | _METRICS[m].allows for m in metrics):
            raise UsageError(f"{flag} is not used by any of: {', '.join(metrics)}.")
    # Checks that depend on the data, not only on which flags are given.
    if (fc_edges is None) != (obs_edges is None):
        raise UsageError("--fc-edges and --obs-edges go together.")
    if member_fraction is not None:
        if not 0 < member_fraction <= 1:
            raise UsageError(f"--member-fraction must be in (0, 1]; got {member_fraction}")
        if not members:
            raise UsageError("--member-fraction needs an ensemble forecast (a number dim).")
    if probability and members:
        raise UsageError(
            "--probability takes one probability per cell, but the forecast has members "
            "(a number dim); drop --probability to score the member fraction."
        )
    if quintiles:
        other = [m for m in metrics if m not in ("rps", "rpss")]
        if other or fc_edges is not None:
            raise UsageError(
                "a forecast of category probabilities (a quintile dim) only gives rps and "
                f"rpss, without edges; drop {', '.join(other) or '--fc-edges/--obs-edges'}."
            )
        if "quintile_threshold" not in fc.coords:
            raise UsageError(
                f"forecast {fc_name!r} has a quintile dim but no quintile_threshold coordinate "
                "(the category edges); rps needs them to place the obs in a category."
            )
    elif {"rps", "rps_clim"} & set(statistics) and (fc_edges is None or not members):
        raise UsageError(
            "rps and rpss need either a forecast of quintile probabilities (pbc-fetch) or an "
            "ensemble (number dim) with --fc-edges and --obs-edges from summarize-dim "
            "--method quantile."
        )

    # Grid spacing must match: the median step of each lat/lon dim both inputs have.
    mismatched = []
    for axis, names in (("latitude", ("latitude", "lat")), ("longitude", ("longitude", "lon"))):
        spacing = [
            np.median(np.abs(np.diff(np.asarray(d[n].values, dtype=float))))
            for d in (forecast, obs)
            for n in names
            if n in d.dims and d.sizes[n] > 1
        ]
        if len(spacing) == 2 and not np.isclose(*spacing, rtol=0.01, atol=1e-6):
            mismatched.append(axis)
    if mismatched:
        raise UsageError(
            "obs grid spacing does not match the forecast on "
            f"{' and '.join(mismatched)}; coarsen --obs onto the forecast "
            "with --reference-grid <forecast.zarr>. Do not "
            "downscale the forecast to the obs grid."
        )

    periods = []
    for p in (fc.attrs.get(AGGREGATION_PERIOD_ATTR), truth.attrs.get(AGGREGATION_PERIOD_ATTR)):
        try:
            periods.append(pd.Timedelta(p) if p is not None else None)
        except ValueError:
            periods.append(str(p).strip())  # calendar periods ("1 month") compare as text
    if None not in periods and periods[0] != periods[1]:
        raise UsageError(
            f"--forecast covers {fc.attrs[AGGREGATION_PERIOD_ATTR]!r} per value but --obs "
            f"{truth.attrs[AGGREGATION_PERIOD_ATTR]!r}; aggregate-temporal both onto the same "
            "windows first."
        )
    # Partial windows (aggregation_coverage < 1) are left out, never scored as complete.
    fc, truth = (
        da.where(da[AGGREGATION_COVERAGE_COORD] >= 1 - 1e-6)
        if AGGREGATION_COVERAGE_COORD in da.coords
        else da
        for da in (fc, truth)
    )

    if "time" in fc.coords and fc["time"].ndim == 0:
        fc = fc.expand_dims("time")
    if "number" in truth.dims:
        # A NaN member leaves the mean NaN (no mean over the members left).
        truth = truth.mean("number", skipna=False, keep_attrs=True)
    truth = _at_valid_times(truth, fc, "--obs")
    units = fc_units = fc.pint.units
    if quintiles:
        # Category probabilities: the units belong to the quintile_threshold edges.
        edge_units = fc["quintile_threshold"].attrs.get("units")
        units = ureg.Unit(edge_units) if edge_units else None
        fc = fc.pint.dequantify()
    elif units is not None:
        if ureg.Quantity(0.0, units).to_base_units().magnitude != 0:
            # Squares and products of °C are meaningless; score offset units as kelvin.
            print(f"verify: scoring in K, not {units:~}.", file=sys.stderr)
            units = ureg.Unit("K")
        fc = fc.pint.to(units).pint.dequantify()
    truth = _to_units(truth, units, f"--obs {obs_name!r}")

    # Side inputs: the climatology, rps edges and filter masks, each paired with the
    # forecast leads like the obs.
    side = []
    if clim is not None:
        name = next(
            (n for n in (variable, f"{fc_name}_avg", fc_name, f"{obs_name}_avg") if n in clim),
            auto_variable(clim),
        )
        side.append(("clim", clim[name], "--clim"))
    for key, eds, role in (
        ("fc_edges", fc_edges, "--fc-edges"),
        ("obs_edges", obs_edges, "--obs-edges"),
    ):
        if eds is None:
            continue
        name = next((n for n in (variable, fc_name, obs_name) if n in eds), auto_variable(eds))
        if "quantile" not in eds[name].dims or "number" in eds[name].dims:
            raise UsageError(
                f"{role} {name!r} has dims {list(eds[name].dims)}; it needs a quantile dim and no "
                "number dim (summarize-dim --dim time --dim number --method quantile -q 0.333 -q 0.667)."
            )
        e = eds[name].sortby("quantile")
        side.append((key, e.drop_vars("quantile").rename(quantile="category_edge"), role))
    for i, mds in enumerate(filter or ()):
        m = mds[auto_variable(mds) or next(iter(mds.data_vars))]
        if "number" in m.dims:
            raise UsageError(f"--filter #{i + 1} has a number dim; a mask applies to every member.")
        side.append((f"filter{i}", m, f"--filter #{i + 1}"))
    extras = {}
    for key, da, role in side:
        da = _at_valid_times(da, fc, role)
        if key.startswith("filter"):
            da = da.pint.dequantify()
            # Missing mask values leave the sample out, as 0 does.
            extras[key] = da.fillna(0) != 0
        else:
            extras[key] = _to_units(da, units, role)
    if fc_edges is not None and (
        extras["fc_edges"].sizes["category_edge"] != extras["obs_edges"].sizes["category_edge"]
    ):
        raise UsageError("--fc-edges and --obs-edges must hold the same number of quantiles.")

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

    cutoff = threshold
    if cutoff is not None and fc_units is not None and units is not None:
        # The cutoff is in the forecast's own units; scoring may have moved them (°C to K).
        cutoff = float(ureg.Quantity(cutoff, fc_units).to(units).magnitude)
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
        else:
            valid = valid & (e.notnull().all("category_edge") if "edges" in key else e.notnull())

    mean = fc.mean("number") if members else fc
    paired = {"fc": fc, "mean": mean, "truth": truth, "error": mean - truth, "bins": bins}
    paired |= {k: v for k, v in extras.items() if not k.startswith("filter")}
    if cutoff is not None:
        paired["obs_event"] = (truth >= cutoff).astype(float)
        if probability:
            paired["prob"] = fc
        else:
            prob = (fc >= cutoff).astype(float)
            paired["prob"] = prob.mean("number") if members else prob
        if member_fraction is not None:
            paired["fc_event"] = paired["prob"] >= member_fraction
        else:
            paired["fc_event"] = mean >= cutoff
    fields = {c: _STATISTICS[c](paired) for c in statistics}

    # One sample set for every statistic: a NaN in any statistic drops that sample from all,
    # so their group means pool the same samples.
    for field in fields.values():
        extra = [d for d in field.dims if d not in valid.dims]
        valid = valid & (field.notnull().all(extra) if extra else field.notnull())
    fields["n_valid"] = xr.ones_like(valid, dtype=float)
    if int(valid.sum()) == 0:
        raise DataError("no sample has both a forecast and an observation (after any --filter).")

    # Group: pool every sample in each group, skipping filtered or missing ones. Sums are
    # taken over every dim not kept, then per --by label, and divided once, so a group's mean
    # is over its samples, not a mean of means. With --lat-weighted, each cell weighs cos(lat).
    for d in keep or ():
        if d not in valid.dims:
            raise UsageError(f"--keep {d!r} is not a dim of the paired data: {list(valid.dims)}.")
    labels = {}
    for b in by or ():
        coord = next((f.coords[b] for f in fields.values() if b in f.coords), None)
        if coord is None:
            raise UsageError(
                f"--by {b!r} is not a label on the forecast; add it with label-group "
                "(--level admin_1, --time MAM,OND) first."
            )
        labels[b] = coord
    label_dims = {d for coord in labels.values() for d in coord.dims}
    if label_dims & set(keep or ()):
        raise UsageError(
            f"--keep {', '.join(sorted(label_dims & set(keep)))} is grouped by --by "
            f"{', '.join(labels)}; keep it or group by it, not both."
        )
    first = [d for d in valid.dims if d not in (keep or ()) and d not in label_dims]
    weight = xr.DataArray(1.0)
    if lat_weighted:
        lat = next((d for d in ("latitude", "lat") if d in valid.dims), None)
        if lat is None:
            raise UsageError("--lat-weighted needs a latitude dim.")
        weight = np.cos(np.deg2rad(valid[lat]))
    grouped = {}
    for c, x in fields.items():
        x = x.where(valid)
        w = 1.0 if c == "n_valid" else weight
        sums = [(x * w).sum(first), (x.notnull() * w).sum(first)]
        if labels:
            sums = [s.groupby(list(labels)).sum() for s in sums]
        total, count = sums
        # n_valid stays a count of the samples pooled.
        grouped[c] = total if c == "n_valid" else total / count
    step = f"{': '.join([*first, *label_dims])}: mean" if first or labels else ""
    step += " (latitude-weighted)" if lat_weighted and step else ""
    step += f" (grouped by {', '.join(labels)})" if labels else ""

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
        "keep": list(keep or ()),
        "by": list(by or ()),
        "lat_weighted": bool(lat_weighted),
    }
    unit_of = {"units": units, "units^2": units**2 if units is not None else None, None: None}
    out = xr.Dataset()
    for m in metrics:
        entry = _METRICS[m]
        with np.errstate(divide="ignore", invalid="ignore"):
            results = entry.function({c: grouped[c] for c in entry.statistics})
        if not isinstance(results, dict):
            results = {m: results}
        u = unit_of[entry.units]
        for name, field in results.items():
            # A zero denominator (no observed event in a group) gives NaN, never ±inf.
            field = field.where(np.isfinite(field)).astype("float32")
            field.attrs = {
                "long_name": entry.names[name],
                "cell_methods": step,
                "verify_metric": m,
                "verify_config": json.dumps(config),
            }
            out[name] = field.pint.quantify("1" if u is None else u)
    out["n_valid"] = grouped["n_valid"].astype("float32")
    out["n_valid"].attrs = {"long_name": "Samples pooled", "units": "1"}
    out.attrs = {"Conventions": "CF-1.13", "verify_metrics": " ".join(metrics)}
    return out


if __name__ == "__main__":
    verify()

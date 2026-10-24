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
_SKILL_VERSION = "0.0.5"

_METRICS = ("hits", "bias", "mae")
_VAR = {"hits": "event_hit", "bias": "bias", "mae": "mae"}


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
    help="Verification metric: hits (event classification), bias (forecast − obs), or mae.",
)
@weather_skill.argument(
    "--threshold",
    type=float,
    default=1.0,
    help="Event cutoff for --metric hits: a cell is an event when the variable is >= this.",
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

    pair = []
    for da in (fc, truth):
        if "number" in da.dims:
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
    if metric != "hits" and threshold != 1.0:
        print(
            f"Note: --threshold {threshold} is ignored for --metric {metric}.",
            file=sys.stderr,
        )

    valid = fc.notnull() & truth.notnull()
    obs_event = None
    if metric == "hits":
        fc_event = fc >= threshold
        obs_event = truth >= threshold
        field = xr.where(fc_event & obs_event, 1, xr.where(fc_event != obs_event, -1, 0))
    elif metric == "bias":
        field = error
    else:
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
            summary = f"MAE {value:.2g}{u_suffix}  ({how})"
    print(f"verify  {summary}")

    out = field.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


if __name__ == "__main__":
    verify()

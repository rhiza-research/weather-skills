# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
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
from weather_skills_core.units import (
    dequantify_dataset,
    parse_aggregation_period,
    ureg,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.8"

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
    default="1",
    help=(
        "Event cutoff for --metric hits: a cell is an event when the variable is >= this. "
        "A bare number is in the forecast's units; '50 mm' on a rate is a total over its "
        "aggregation_period."
    ),
)
@weather_skill.argument(
    "--member-fraction",
    type=float,
    help=(
        "--metric hits on an ensemble: the forecast event is at least this share of members "
        ">= --threshold (0.5 = half). Default: the ensemble mean >= --threshold."
    ),
)
def verify(forecast, obs, variable, metric, threshold, member_fraction, **kwargs):
    """Score a forecast against observations cell by cell: event hits, bias, or mean absolute error."""
    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )

    fc, truth = forecast[fc_name], obs[obs_name]
    if member_fraction is not None:
        if metric != "hits":
            raise UsageError("--member-fraction is for --metric hits.")
        if not 0 < member_fraction <= 1:
            raise UsageError(f"--member-fraction must be in (0, 1]; got {member_fraction}")
        if "number" not in fc.dims:
            raise UsageError("--member-fraction needs an ensemble forecast (a number dim).")

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
        # --member-fraction counts members, so keep the forecast's.
        if "number" in da.dims and not (member_fraction is not None and da is fc):
            # A NaN member leaves the mean NaN (no mean over the members left).
            da = da.mean("number", skipna=False, keep_attrs=True)
        # A scalar time (after select) becomes a length-1 dim so the inner join
        # below checks it against the other input instead of dropping it.
        if "time" in da.coords and da["time"].ndim == 0:
            da = da.expand_dims("time")
        pair.append(da)
    fc, truth = pair
    if "step" in fc.dims and "step" not in truth.dims:
        # Score each lead: pair forecast (init, step) with the obs at init + step.
        if "time" not in fc.dims or "time" not in truth.dims:
            raise UsageError(
                "forecast has a step axis but no init time, or --obs has no time axis; "
                "cannot match leads to obs dates."
            )
        valid = (fc["time"] + fc["step"]).transpose("time", "step")
        pos = pd.Index(truth["time"].values).get_indexer(valid.values.ravel())
        pos = pos.reshape(valid.shape)
        if (pos < 0).all():
            raise UsageError(
                f"no --obs time matches a forecast valid time (init + step): valid "
                f"{pd.Timestamp(valid.values.min())} … {pd.Timestamp(valid.values.max())} vs obs "
                f"{pd.Timestamp(truth['time'].values.min())} … "
                f"{pd.Timestamp(truth['time'].values.max())}. Label both with the same bins: "
                "obs windows start on the day each forecast step starts."
            )
        attrs = truth.attrs
        picked = truth.drop_vars(
            [c for c in truth.coords if c != "time" and "time" in truth[c].dims]
        ).isel(time=xr.DataArray(np.maximum(pos, 0), dims=("init", "step")))
        truth = picked.drop_vars("time").rename(init="time")
        # Valid times past the obs record stay NaN: not scored, never filled.
        truth = truth.where(xr.DataArray(pos >= 0, dims=("time", "step")))
        truth = truth.assign_coords(time=fc["time"].values, step=fc["step"].values)
        truth.attrs = attrs
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
    if metric != "hits" and threshold != "1":
        print(
            f"Note: --threshold {threshold} is ignored for --metric {metric}.",
            file=sys.stderr,
        )

    cutoff = None
    if metric == "hits":
        try:
            cutoff = float(threshold)
        except ValueError:
            try:
                quantity = ureg.Quantity(threshold)
            except Exception:  # noqa: BLE001 — any pint parse failure is a usage error
                raise UsageError(
                    f"--threshold {threshold!r} is not a number or a quantity"
                ) from None
            if not u_fc:
                raise UsageError(
                    f"--threshold {threshold!r} has units but the forecast has none."
                ) from None
            try:
                cutoff = quantity.to(ureg.Unit(u_fc)).magnitude
            except pint.DimensionalityError:
                # A total ("50 mm") on a rate: spread it over the forecast's window.
                period = fc.attrs.get("aggregation_period") or fc.attrs.get("data_interval")
                if not period:
                    raise UsageError(
                        f"--threshold {threshold!r} is a total but the forecast ({u_fc}) has no "
                        "aggregation_period or data_interval to spread it over."
                    ) from None
                try:
                    cutoff = (quantity / parse_aggregation_period(period)).to(ureg.Unit(u_fc))
                except pint.DimensionalityError:
                    raise UsageError(
                        f"--threshold {threshold!r} cannot be compared with {u_fc}."
                    ) from None
                cutoff = cutoff.magnitude
                print(
                    f"--threshold {threshold} over {period} = {cutoff:.6g} {ureg.Unit(u_fc):~}.",
                    file=sys.stderr,
                )
        cutoff = float(cutoff)

    if member_fraction is not None:
        # Share of members at or above the cutoff; a NaN member leaves the cell NaN.
        share = (fc >= cutoff).where(fc.notnull()).mean("number", skipna=False)
        fc = share
        fc_event = share >= member_fraction
    elif metric == "hits":
        fc_event = fc >= cutoff
    valid = fc.notnull() & truth.notnull()
    obs_event = None
    if metric == "hits":
        obs_event = truth >= cutoff
        field = xr.where(
            fc_event & obs_event,
            1,
            xr.where(obs_event, 2, xr.where(fc_event, 3, 0)),
        )
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
            flag_values=np.array([0, 1, 2, 3], dtype=np.int8),
            flag_meanings="correct_negative hit miss false_alarm",
            event_threshold=cutoff,
            event_variable=fc_name if fc_name == obs_name else f"{fc_name},{obs_name}",
        )
        if member_fraction is not None:
            attrs["event_member_fraction"] = member_fraction
    elif metric == "bias":
        attrs.update(
            long_name="Forecast bias (forecast − observation)", units=error.attrs.get("units")
        )
    else:
        attrs.update(long_name="Mean absolute error", units=error.attrs.get("units"))
    field.attrs = attrs

    def _summary(field):
        finite = field.notnull()
        if metric == "hits":
            n_hit = int((field == 1).sum())
            n_miss = int((field == 2).sum())
            n_false = int((field == 3).sum())
            n_obs = n_hit + n_miss
            if n_obs == 0:
                text = "hit rate n/a  (0 obs events)"
            else:
                text = f"hit rate {100 * n_hit / n_obs:.0f}%  ({n_hit}/{n_obs} obs events)"
            far = f"{100 * n_false / (n_hit + n_false):.0f}%" if n_hit + n_false else "n/a"
            csi = f"{n_hit / (n_obs + n_false):.2f}" if n_obs + n_false else "n/a"
            return f"{text}, false alarms {n_false} (FAR {far}), CSI {csi}"
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
            return f"{metric} n/a  (no finite cells)"
        if metric == "bias":
            sign = "+" if value >= 0 else ""
            return f"bias {sign}{value:.2g}{u_suffix}  ({how})"
        return f"MAE {value:.2g}{u_suffix}  ({how})"

    summary = _summary(field)
    if "step" in field.dims and field.sizes["step"] > 1:
        for step in field["step"].values:
            line = f"step {pd.Timedelta(step)}: {_summary(field.sel(step=step))}"
            print(f"verify  {line}")
            summary += f"; {line}"
    print(f"verify  {summary.split('; ')[0]}")

    out = field.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


if __name__ == "__main__":
    verify()

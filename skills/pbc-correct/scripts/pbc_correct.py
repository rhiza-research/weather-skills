# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.2",
#   "pandas",
# ]
# ///
"""Probabilistic bias correction (PBC) of an ensemble forecast's quintile probabilities."""

import sys

import numpy as np
import pandas as pd
import pint
import pint_xarray
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    dequantify_dataset,
    parse_aggregation_period,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_COMPONENTS = ("raw", "debias", "persistence", "climatology")
_QUINTILES = np.array([0.2, 0.4, 0.6, 0.8])
_MIN_SAMPLES = 10
# Tiny ridge so a cell whose lagged indicators never change still solves.
_RIDGE = 1e-6


def _project(cdf):
    """Clip to [0, 1] and make the 4 CDF values non-decreasing (isotonic regression).

    For n points with equal weights the isotonic fit at i is
    max over j <= i of min over k >= i of mean(cdf[j..k]); n = 4 makes that cheap.
    """
    cdf = np.clip(cdf, 0.0, 1.0)
    n = cdf.shape[0]
    out = np.empty_like(cdf)
    for i in range(n):
        best = None
        for j in range(i + 1):
            low = None
            for k in range(i, n):
                m = cdf[j : k + 1].mean(axis=0)
                low = m if low is None else np.minimum(low, m)
            best = low if best is None else np.maximum(best, low)
        out[i] = best
    return out


def _doy_distance(dates, target):
    """Days between calendar days, wrapping across the year end."""
    d = np.abs(pd.DatetimeIndex(dates).day_of_year.values - pd.Timestamp(target).day_of_year)
    return np.minimum(d, 366 - d)


@weather_skill(name="pbc-correct", version=_SKILL_VERSION)
@weather_skill.argument("--forecast", type=Dataset("any"), required=True)
@weather_skill.argument("--hindcast", type=Dataset("any"), required=True)
@weather_skill.argument("--obs", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--step",
    type=int,
    help="Lead in days of the window to correct (a step label of the forecast). "
    "Default: the forecast's only step.",
)
@weather_skill.argument(
    "--components",
    default="debias,persistence,climatology",
    help="Comma list of corrections to average: debias, persistence, climatology, "
    "or raw alone for the uncorrected ensemble on the same quintiles.",
)
@weather_skill.argument(
    "--years", type=int, default=20, help="Years of hindcasts and observations used to train."
)
@weather_skill.argument(
    "--margin",
    type=int,
    default=35,
    help="Days either side of the target calendar day pooled into training.",
)
@weather_skill.argument(
    "--clim-years",
    type=int,
    default=10,
    help="Most recent years of observations behind Climatology++.",
)
@weather_skill.argument(
    "--weights",
    choices=["equal", "skill"],
    default="equal",
    help="Blend components equally, or with the weights that minimise the blend's past RPS "
    "on the training hindcasts.",
)
def pbc_correct(
    forecast,
    hindcast,
    obs,
    variable,
    step,
    components,
    years,
    margin,
    clim_years,
    weights,
    **kwargs,
):
    """Probabilistic bias correction (PBC) of an ensemble forecast's quintile probabilities."""
    parts = [c.strip() for c in components.split(",") if c.strip()]
    unknown = [c for c in parts if c not in _COMPONENTS]
    if not parts or unknown:
        raise UsageError(
            f"--components {components!r}: choose from {', '.join(_COMPONENTS)} (comma separated)."
        )
    if "raw" in parts and len(parts) > 1:
        raise UsageError("--components raw is the uncorrected baseline; pass it alone.")

    name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for ds, role, n in ((forecast, "forecast", name), (hindcast, "hindcast", name)):
        if not n or n not in ds:
            raise UsageError(f"variable {n!r} missing from --{role}: {list(ds.data_vars)}")
        missing = [d for d in ("step", "number") if d not in ds[n].dims]
        if missing or "time" not in ds[n].coords:
            raise UsageError(
                f"--{role} {n!r} needs a time (init) coord and step and number dims; "
                f"missing {missing or ['time']}. Keep ensemble members, and stack inits "
                "with `concat --dim time`."
            )
    if not obs_name or obs_name not in obs:
        raise UsageError(f"variable {obs_name!r} missing from --obs: {list(obs.data_vars)}")

    period = forecast[name].attrs.get(AGGREGATION_PERIOD_ATTR)
    obs_period = obs[obs_name].attrs.get(AGGREGATION_PERIOD_ATTR)
    if not period:
        raise UsageError(
            f"--forecast {name!r} has no {AGGREGATION_PERIOD_ATTR}; roll its leads into "
            "windows first: aggregate-temporal --period weekly --time-dim step."
        )
    if not obs_period or parse_aggregation_period(obs_period) != parse_aggregation_period(period):
        raise UsageError(
            f"--obs {obs_name!r} must be {period} means labeled every day at the window "
            f"start (found {AGGREGATION_PERIOD_ATTR}={obs_period!r}); run aggregate-temporal "
            "--window <length> --align left on the daily obs (e.g. --window weekly)."
        )
    window = pd.Timedelta(days=float(parse_aggregation_period(period).to("day").magnitude))

    steps = pd.TimedeltaIndex(forecast["step"].values)
    if step is None:
        if len(steps) != 1:
            raise UsageError(
                f"--forecast has {len(steps)} steps ({', '.join(str(s.days) for s in steps)} "
                "days); pick one with --step."
            )
        lead = steps[0]
    else:
        lead = pd.Timedelta(days=step)
        if lead not in steps or lead not in pd.TimedeltaIndex(hindcast["step"].values):
            raise UsageError(f"--step {step} is not a step of both --forecast and --hindcast.")

    fc = forecast[name].sel(step=lead)
    hc = hindcast[name].sel(step=lead)
    # A single init (pbc-fetch, ecmwf-fetch) carries time as a scalar coord.
    fc = fc if "time" in fc.dims else fc.expand_dims("time")
    hc = hc if "time" in hc.dims else hc.expand_dims("time")
    truth = obs[obs_name]
    if AGGREGATION_COVERAGE_COORD in truth.coords:
        # A window missing any day is not an observation: never scored or trained on.
        truth = truth.where(truth[AGGREGATION_COVERAGE_COORD] >= 1 - 1e-9)
    # Thresholds and members are compared in the obs units.
    units = variable_units(truth)
    if bool(units) != bool(variable_units(fc)):
        raise UsageError(
            f"--forecast {name!r} units={variable_units(fc)!r} but --obs {obs_name!r} "
            f"units={units!r}; both need units (or neither)."
        )
    if units:
        try:
            fc, hc = fc.pint.to(truth.pint.units), hc.pint.to(truth.pint.units)
        except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
            raise UsageError(
                f"--forecast {name!r} ({variable_units(fc)}) and --obs {obs_name!r} "
                f"({units}) carry incompatible units; give both as rates (mm day-1)."
            ) from None
        fc, hc, truth = (dequantify_dataset(da.to_dataset(name="v"))["v"] for da in (fc, hc, truth))
        units = truth.attrs["units"]
    fc, hc, truth = xr.align(fc, hc, truth, join="inner", exclude=["time", "number"])
    if fc.sizes["latitude"] == 0 or fc.sizes["longitude"] == 0:
        raise UsageError(
            "--forecast, --hindcast and --obs share no grid cells; put them on one grid "
            "with coarsen --reference-grid."
        )

    obs_dates = pd.DatetimeIndex(truth["time"].values)
    obs_vals = truth.transpose("time", "latitude", "longitude").values.astype(float)
    obs_index = {d: i for i, d in enumerate(obs_dates)}
    hc_inits = pd.DatetimeIndex(hc["time"].values)
    hc_vals = hc.transpose("time", "number", "latitude", "longitude").values.astype(float)
    fc_inits = pd.DatetimeIndex(fc["time"].values)
    fc_vals = fc.transpose("time", "number", "latitude", "longitude").values.astype(float)
    grid = obs_vals.shape[1:]
    nan_cdf = np.full((4, *grid), np.nan)

    # Quintile edges per calendar day, from observation windows that ended before the
    # first forecast init: the same edges for every init, and none taken from the future.
    base_end = fc_inits.min()
    base = (obs_dates + window <= base_end) & (obs_dates >= base_end - pd.DateOffset(years=years))
    thresholds = {}

    def edges(date):
        doy = pd.Timestamp(date).day_of_year
        if doy not in thresholds:
            pool = base & (_doy_distance(obs_dates, date) <= margin)
            if pool.sum() < _MIN_SAMPLES:
                thresholds[doy] = nan_cdf
            else:
                thresholds[doy] = np.nanquantile(obs_vals[pool], _QUINTILES, axis=0)
        return thresholds[doy]

    def observed(date):
        """Indicator that the observed window starting on date is below each edge."""
        i = obs_index.get(pd.Timestamp(date))
        if i is None:
            return nan_cdf
        value, edge = obs_vals[i], edges(date)
        return np.where(np.isfinite(value) & np.isfinite(edge), value <= edge, np.nan)

    def members_below(members, date):
        """Fraction of finite members at or below each edge."""
        edge = edges(date)
        finite = np.isfinite(members)
        count = finite.sum(axis=0)
        below = (finite[None] & (members[None] <= edge[:, None])).sum(axis=1)
        return np.where((count > 0) & np.isfinite(edge), below / np.maximum(count, 1), np.nan)

    hc_valid = hc_inits + lead

    def correct(init, members):
        """Each chosen component's corrected CDF for one init, trained only on its past.

        Returns the components, the training hindcast indices and their observed events.
        """
        valid = init + lead
        raw = members_below(members, valid)

        # Training: hindcasts whose target window was fully observed before this init,
        # near the same calendar day, within the last --years years.
        train = np.flatnonzero(
            (hc_valid + window <= init)
            & (hc_valid >= init - pd.DateOffset(years=years))
            & (_doy_distance(hc_valid, valid) <= margin)
        )
        f_h = np.empty((len(train), 4, *grid))
        y_h = np.empty((len(train), 4, *grid))
        for n, i in enumerate(train):
            f_h[n] = members_below(hc_vals[i], hc_valid[i])
            y_h[n] = observed(hc_valid[i])

        corrected = []
        if "raw" in parts:
            corrected.append(raw)
        if "debias" in parts:
            error = y_h - f_h
            count = np.isfinite(error).sum(axis=0)
            shift = np.nansum(error, axis=0) / np.maximum(count, 1)
            corrected.append(_project(np.where(count >= _MIN_SAMPLES, raw + shift, np.nan)))
        if "persistence" in parts:
            # Regress the observed indicator on the raw probability and the two windows
            # observed just before init, separately for each cell and edge.
            def lags(start):
                return [observed(start - window), observed(start - 2 * window)]

            x_h = np.empty((len(train), 4, *grid, 4))
            for n, i in enumerate(train):
                x_h[n] = np.stack([np.ones_like(f_h[n]), f_h[n], *lags(hc_inits[i])], axis=-1)
            ok = np.isfinite(x_h).all(axis=-1) & np.isfinite(y_h)
            x_h = np.where(ok[..., None], x_h, 0.0)
            y_fit = np.where(ok, y_h, 0.0)
            xtx = np.einsum("n...i,n...j->...ij", x_h, x_h) + _RIDGE * np.eye(4)
            xty = np.einsum("n...i,n...->...i", x_h, y_fit)
            coef = np.linalg.solve(xtx, xty[..., None])[..., 0]
            x_0 = np.stack([np.ones_like(raw), raw, *lags(init)], axis=-1)
            fit = (coef * x_0).sum(axis=-1)
            corrected.append(_project(np.where(ok.sum(axis=0) >= _MIN_SAMPLES, fit, np.nan)))
        if "climatology" in parts:
            # Climatology++: how often the last --clim-years years fell below each edge.
            recent = (
                (obs_dates + window <= init)
                & (obs_dates >= init - pd.DateOffset(years=clim_years))
                & (_doy_distance(obs_dates, valid) <= margin)
            )
            values = obs_vals[recent]
            finite = np.isfinite(values)
            count = finite.sum(axis=0)
            edge = edges(valid)
            below = (finite[None] & (values[None] <= edge[:, None])).sum(axis=1)
            enough = (count >= _MIN_SAMPLES) & np.isfinite(edge)
            clim = np.where(enough, below / np.maximum(count, 1), np.nan)
            corrected.append(_project(clim))

        return corrected, train, y_h

    past = {}  # hindcast index -> its components, for --weights skill

    def weights_for(train, y_h):
        """Blend weights that minimise the blend's RPS on the training hindcasts.

        Each training hindcast is corrected from its own past, so the fit is out of
        sample. RPS is a sum of squared CDF errors, so the best non-negative weights
        summing to 1 solve a small least-squares problem; every subset of components
        is tried and the best feasible one kept.
        """
        k = len(parts)
        gram, cross = np.zeros((k, k)), np.zeros(k)
        for n, i in enumerate(train):
            if i not in past:
                past[i] = correct(hc_inits[i], hc_vals[i])[0]
            comps = np.stack(past[i])
            ok = np.isfinite(comps).all(axis=(0, 1)) & np.isfinite(y_h[n]).all(axis=0)
            c = np.where(ok, comps, 0.0).reshape(k, -1)
            gram += c @ c.T
            cross += c @ np.where(ok, y_h[n], 0.0).ravel()
        best, best_loss = np.full(k, 1.0 / k), np.inf
        if not gram.any():
            return best
        for mask in range(1, 2**k):
            use = [j for j in range(k) if mask >> j & 1]
            m = len(use)
            # Minimise w'Gw - 2c'w over the chosen components with sum(w) = 1.
            system = np.zeros((m + 1, m + 1))
            system[:m, :m] = gram[np.ix_(use, use)]
            system[:m, m] = system[m, :m] = 1.0
            rhs = np.append(cross[use], 1.0)
            try:
                sol = np.linalg.solve(system, rhs)[:m]
            except np.linalg.LinAlgError:
                continue
            if (sol < -1e-12).any():
                continue
            w = np.zeros(k)
            w[use] = np.clip(sol, 0, None)
            loss = w @ gram @ w - 2 * cross @ w
            if loss < best_loss:
                best, best_loss = w / w.sum(), loss
        return best

    probabilities, edge_rows, notes, used_weights = [], [], [], []
    for t, init in enumerate(fc_inits):
        valid = init + lead
        corrected, train, y_h = correct(init, fc_vals[t])
        if weights == "skill" and len(parts) > 1:
            w = weights_for(train, y_h)
            used_weights.append(w)
            blend = np.tensordot(w, np.stack(corrected), axes=1)
            # A cell where a weighted component is NaN stays NaN, as with equal weights.
            blend = np.where(np.isfinite(np.stack(corrected)[w > 0]).all(axis=0), blend, np.nan)
            corrected = [blend]
        cdf = corrected[0] if parts == ["raw"] else _project(np.mean(corrected, axis=0))
        if len(train) < _MIN_SAMPLES and parts != ["raw"] and parts != ["climatology"]:
            notes.append(f"{init:%Y-%m-%d}: {len(train)} training hindcasts")
        full = np.concatenate([cdf, np.where(np.isfinite(cdf[:1]), 1.0, np.nan)])
        probabilities.append(np.diff(full, axis=0, prepend=0.0))
        edge_rows.append(np.concatenate([edges(valid), np.full((1, *grid), np.inf)]))

    if used_weights:
        mean_w = np.mean(used_weights, axis=0)
        print(
            "pbc-correct: mean skill weights "
            + ", ".join(f"{p} {w:.2f}" for p, w in zip(parts, mean_w, strict=True)),
            file=sys.stderr,
        )
    if notes:
        print(
            f"pbc-correct: fewer than {_MIN_SAMPLES} hindcasts, left NaN: " + "; ".join(notes),
            file=sys.stderr,
        )

    valid_times = (fc_inits + lead).values
    quintile = np.array([0.2, 0.4, 0.6, 0.8, 1.0])
    dims = ("time", "quintile", "latitude", "longitude")
    out = xr.Dataset(
        {name: (dims, np.stack(probabilities).astype("float32"))},
        coords={
            "time": valid_times,
            "quintile": quintile,
            "latitude": truth["latitude"].values,
            "longitude": truth["longitude"].values,
            "forecast_reference_time": ("time", fc_inits.values),
            "quintile_threshold": (dims, np.stack(edge_rows).astype("float32")),
        },
    )
    out[name].attrs = {
        "long_name": f"{name} quintile probability",
        "units": "1",
        AGGREGATION_PERIOD_ATTR: period,
        "pbc_components": ",".join(parts),
        "pbc_weights": weights,
        "pbc_lead": f"{lead.days} day",
    }
    if used_weights:
        out[name].attrs["pbc_mean_weights"] = np.mean(used_weights, axis=0).round(4).tolist()
    out["quintile"].attrs.update(long_name="quintile upper bound", units="1")
    out["quintile_threshold"].attrs.update(
        long_name="upper edge of the observed quintile", units=units or ""
    )
    out["forecast_reference_time"].attrs.update(standard_name="forecast_reference_time")
    out["time"].attrs.update(standard_name="time", axis="T", long_name="start of valid window")
    for axis in ("latitude", "longitude"):
        out[axis].attrs = dict(truth[axis].attrs)
    out.attrs["Conventions"] = "CF-1.13"
    print(
        f"pbc-correct  {len(fc_inits)} init(s), lead {lead.days} d, {period} window, "
        f"components {','.join(parts)}, {weights} weights"
    )
    return out


if __name__ == "__main__":
    pbc_correct()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.3",
#   "poold==0.0.5",
# ]
# ///
"""Learn weights for an ensemble of expert forecasts by online learning under delayed obs."""

import warnings

import numpy as np
import pint
import pint_xarray
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_utils import latitude_weights
from weather_skills_core.units import dequantify_dataset, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_LEARNERS = ("dormplus", "dorm", "adahedged", "dub")
_HINTS = ("recent_g", "mean_g", "none")


@weather_skill(
    name="online-ensemble",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--forecast", type=Dataset("any"), required=True)
@weather_skill.argument("--obs", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--model-dim",
    default="model",
    help="Dim of --forecast that holds the expert forecasts.",
)
@weather_skill.argument(
    "--delay",
    type=int,
    required=True,
    help="Rounds between issuing a forecast and seeing its obs (>= 1).",
)
@weather_skill.argument(
    "--learner",
    choices=list(_LEARNERS),
    default="dormplus",
    help="Online learning algorithm: dormplus, dorm, adahedged, or dub.",
)
@weather_skill.argument(
    "--hint",
    choices=list(_HINTS),
    default="recent_g",
    help="Optimistic hint for the rounds still waiting on obs: recent_g, mean_g, or none.",
)
def online_ensemble(forecast, obs, variable, model_dim, delay, learner, hint, **kwargs):
    """Learn weights for an ensemble of expert forecasts by online learning under delayed obs."""
    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )
    if delay < 1:
        raise UsageError(f"--delay must be at least 1 round, got {delay}.")

    fc, truth = forecast[fc_name], obs[obs_name]
    if model_dim not in fc.dims:
        raise UsageError(
            f"--forecast {fc_name!r} has no {model_dim!r} dim (dims: {list(fc.dims)}); stack "
            "the expert forecasts along one with concat --dim model --coords a,b,..., or name "
            "the dim with --model-dim."
        )
    if fc.sizes[model_dim] < 2:
        raise UsageError(f"--forecast needs at least 2 experts along {model_dim!r}.")
    if "step" in fc.dims:
        raise UsageError(
            "forecast still has a step axis; run step-to-time first so each round is "
            "labeled by its valid time."
        )
    for da, role in ((fc, "forecast"), (truth, "obs")):
        if "time" not in da.dims:
            raise UsageError(f"--{role} {da.name!r} needs a time dim with one entry per round.")
    times = fc["time"].values
    if fc.sizes["time"] < 2 or not (times[1:] > times[:-1]).all():
        raise UsageError("--forecast time must hold at least 2 strictly increasing rounds.")

    pair = []
    for da in (fc, truth):
        if "number" in da.dims:
            # A NaN member leaves the mean NaN (no mean over the members left).
            da = da.mean("number", skipna=False, keep_attrs=True)
        pair.append(da)
    fc, truth = pair
    u_fc, u_obs = variable_units(fc), variable_units(truth)
    if bool(u_fc) != bool(u_obs):
        raise UsageError(
            f"--forecast {fc_name!r} units={u_fc!r} but --obs {obs_name!r} units={u_obs!r}; "
            "both inputs need units (or neither) to be compared."
        )
    if u_fc and u_obs:
        try:
            truth = truth.pint.to(fc.pint.units)
        except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup):
            raise UsageError(
                f"--forecast {fc_name!r} ({u_fc}) and --obs {obs_name!r} ({u_obs}) carry "
                "incompatible units (e.g. a total against a rate); put both on one basis "
                "with aggregate-temporal / convert-to-totals first."
            ) from None
    fc, truth = (
        dequantify_dataset(da.to_dataset(name="v"))["v"] if da.pint.units is not None else da
        for da in (fc, truth)
    )

    space = [d for d in fc.dims if d not in ("time", model_dim)]
    extra = [d for d in truth.dims if d != "time" and d not in space]
    if extra:
        raise UsageError(f"--obs has dims {extra} that --forecast lacks.")
    fc, truth = xr.align(fc, truth, join="inner", exclude=["time", model_dim])
    empty = [d for d in space if fc.sizes[d] == 0]
    if empty:
        raise UsageError(
            f"no overlapping {', '.join(empty)} labels between --forecast and --obs; for "
            "lat/lon, coarsen --obs --reference-grid <forecast.zarr>."
        )
    # Obs on the forecast rounds; a round without obs never gives feedback.
    truth = truth.reindex(time=fc["time"])

    models = [str(m) for m in fc[model_dim].values]
    n_round, n_model = fc.sizes["time"], len(models)
    X = fc.transpose("time", *space, model_dim).values.astype(float).reshape(n_round, -1, n_model)
    Y = truth.transpose("time", *space).values.astype(float).reshape(n_round, -1)
    lat = next((n for n in ("latitude", "lat") if n in space), None)
    if lat is None:
        area = np.ones(Y.shape[1])
    else:
        area = (
            latitude_weights(fc[lat])
            .broadcast_like(truth.isel(time=0, drop=True))
            .transpose(*space)
            .values.reshape(-1)
        )

    labels = np.datetime_as_string(times, unit="D") if times.dtype.kind == "M" else times
    for t in range(n_round):
        missing = [m for i, m in enumerate(models) if not np.isfinite(X[t, :, i]).any()]
        if missing:
            raise UsageError(
                f"--forecast has no data for {missing} at {labels[t]}; every expert needs a "
                "forecast for every round."
            )
    # Cells scored in each round: obs and every expert finite.
    valid = np.isfinite(X).all(axis=2) & np.isfinite(Y)
    scored = valid.any(axis=1)
    if not scored.any():
        raise UsageError(
            "--obs has no data on any --forecast round; time labels must match exactly "
            f"(forecast {labels[0]} … {labels[-1]})."
        )
    last_scored = int(np.flatnonzero(scored)[-1])
    gaps = [str(labels[t]) for t in range(last_scored) if not scored[t]]
    if gaps:
        raise UsageError(
            f"--obs has no data for rounds {gaps} but has data for later rounds; the learner "
            "needs obs for every round up to the newest one it scores. Drop those rounds "
            "from --forecast or supply their obs."
        )

    def rmse(w, Xc, yc, ac):
        return float(np.sqrt(np.sum(ac * (Xc @ w - yc) ** 2)))

    def rmse_grad(w, Xc, yc, ac):
        err = Xc @ w - yc
        loss = np.sqrt(np.sum(ac * err**2))
        if np.isclose(loss, 0.0):
            return np.zeros(n_model)
        return Xc.T @ (ac * err) / loss

    # Loss of each scored round as a function of the play w, as poold expects.
    losses = {}
    for t in np.flatnonzero(scored):
        cells = valid[t]
        args = {"Xc": X[t, cells], "yc": Y[t, cells], "ac": area[cells] / area[cells].sum()}
        losses[int(t)] = {
            "fun": lambda w, a=args: rmse(w, **a),
            "grad": lambda w, a=args: rmse_grad(w, **a),
        }

    with warnings.catch_warnings():
        # poold's plotting module has invalid escape sequences in its strings.
        warnings.simplefilter("ignore", SyntaxWarning)
        import poold
        from poold.utils import loss_regret
    ol = poold.create(learner, model_list=models)
    # DORM and DORM+ play in regret space, so their hints are regrets; AdaHedgeD and
    # DUB take gradient hints.
    regret_hint = learner in ("dorm", "dormplus")
    fed = []
    weights = np.zeros((n_round, n_model))
    with warnings.catch_warnings():
        # AdaHedgeD divides by a zero hint error when a hint is exact.
        warnings.simplefilter("ignore", RuntimeWarning)
        for t in range(n_round):
            os_times = sorted(ol.get_outstanding())
            losses_fb = [(s, losses[s]) for s in os_times if s + delay <= t and s in losses]
            ol.record_losses(losses_fb)
            fed += [s for s, _ in losses_fb]
            pending = [s for s in os_times if s not in fed]
            h = None
            if hint != "none" and fed:
                grads = [ol.get_grad(s) for s in fed]
                g_tilde = grads[-1] if hint == "recent_g" else np.mean(grads, axis=0)
                h_vec = np.zeros(n_model)
                for s in pending:
                    if regret_hint:
                        h_vec += loss_regret(g_tilde, ol.get_play(s), ol.groups)
                    else:
                        h_vec += g_tilde
                h = {"grad": lambda w, h_vec=h_vec: h_vec}
            weights[t] = ol.update_and_play(losses_fb, hint=h)

    expert_rmse = np.full((n_round, n_model), np.nan)
    ensemble_rmse = np.full(n_round, np.nan)
    for t, loss in losses.items():
        ensemble_rmse[t] = loss["fun"](weights[t])
        for i in range(n_model):
            expert_rmse[t, i] = loss["fun"](np.eye(n_model)[i])

    weight = xr.DataArray(
        weights,
        dims=("time", model_dim),
        coords={"time": fc["time"], model_dim: fc[model_dim]},
        attrs={"long_name": "Online ensemble weight", "units": "1"},
    )
    ens = (fc * weight).sum(model_dim, skipna=False)
    ens.attrs = dict(forecast[fc_name].attrs)
    units = fc.attrs.get("units")
    if units:
        ens.attrs["units"] = units
    ens.attrs["ensemble_method"] = f"poold {learner}, hint {hint}, delay {delay}"
    ens.attrs["comment"] = f"Online-learned weighted sum of the {model_dim} forecasts."
    rmse_attrs = {"units": units} if units else {}
    out = xr.Dataset(
        {
            fc_name: ens,
            "weight": weight,
            "expert_rmse": xr.DataArray(
                expert_rmse,
                dims=("time", model_dim),
                attrs={"long_name": "Expert RMSE over the round's cells", **rmse_attrs},
            ),
            "ensemble_rmse": xr.DataArray(
                ensemble_rmse,
                dims=("time",),
                attrs={"long_name": "Online ensemble RMSE over the round's cells", **rmse_attrs},
            ),
        }
    )

    u = f" {units}" if units else ""
    mean_expert = np.nanmean(expert_rmse, axis=0)
    # Mean per scored round, so it reads against the mean RMSEs.
    regret = np.nanmean(ensemble_rmse) - mean_expert.min()
    summary = (
        f"{learner} + {hint} hint, delay {delay}: {n_round} rounds, {int(scored.sum())} scored. "
        f"Mean RMSE ensemble {np.nanmean(ensemble_rmse):.3g}{u}, "
        + ", ".join(f"{m} {v:.3g}" for m, v in zip(models, mean_expert, strict=True))
        + f"; mean regret vs best expert {regret:+.3g}{u}. Weights for {labels[-1]}: "
        + ", ".join(f"{m} {v:.2f}" for m, v in zip(models, weights[-1], strict=True))
    )
    print(f"online-ensemble  {summary}")
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["online_ensemble_summary"] = summary
    return out


if __name__ == "__main__":
    online_ensemble()

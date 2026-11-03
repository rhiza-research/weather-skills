# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.3",
#   "poold==0.0.5",
# ]
# ///
"""Learn weights for an ensemble of expert forecasts by online learning under delayed obs."""

import json
import sys
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
@weather_skill.argument(
    "--state-in",
    help="Learner state JSON from an earlier run's --state-out; resume from it.",
)
@weather_skill.argument(
    "--state-out",
    help="Write the learner state after the last round to this JSON file.",
)
def online_ensemble(
    forecast, obs, variable, model_dim, delay, learner, hint, state_in, state_out, **kwargs
):
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
    if not (times[1:] > times[:-1]).all():
        raise UsageError("--forecast time must be strictly increasing, one entry per round.")

    # Round numbers count from the first round the learner ever played, so a resumed
    # run keeps counting --delay across runs.
    keys = [str(v) for v in times]
    state = None
    if state_in:
        with open(state_in) as f:
            state = json.load(f)
        saved = {k: state[k] for k in ("learner", "hint", "delay")}
        if saved != {"learner": learner, "hint": hint, "delay": delay}:
            raise UsageError(
                f"--state-in was written with {saved}; pass the same --learner, --hint "
                "and --delay to resume."
            )
        waiting = state["waiting"]
        missing = [k for k in waiting if k not in keys]
        if missing:
            raise UsageError(
                f"--forecast lacks rounds {missing} that --state-in is still waiting on; "
                "include them so their obs can score them."
            )
        last = keys.index(state["last_round"])
        keep = [i for i, k in enumerate(keys) if k in waiting or i > last]
        if len(keep) < len(keys):
            print(
                f"Note: skipping {len(keys) - len(keep)} rounds that --state-in already finished.",
                file=sys.stderr,
            )
        fc, times = fc.isel(time=keep), times[keep]
        keys = [keys[i] for i in keep]
        round_no = []
        next_round = state["poold"]["learner"]["t"]
        for k in keys:
            if k in waiting:
                round_no.append(waiting[k])
            else:
                round_no.append(next_round)
                next_round += 1
    else:
        round_no = list(range(len(keys)))
    if not keys:
        raise UsageError("--forecast has no rounds to play.")

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
    if state and state["models"] != models:
        raise UsageError(
            f"--forecast {model_dim} is {models} but --state-in was written for "
            f"{state['models']}; the experts and their order must match."
        )
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
    # An expert with no data in a round sits that round out.
    present = np.isfinite(X).any(axis=1)
    empty_rounds = [str(labels[t]) for t in range(n_round) if not present[t].any()]
    if empty_rounds:
        raise UsageError(f"--forecast has no expert with data for rounds {empty_rounds}.")
    never = [m for i, m in enumerate(models) if not present[:, i].any()]
    if never:
        print(f"Note: {never} have no data yet; they join when they do.", file=sys.stderr)
    # Cells scored in each round: obs and every expert in that round finite.
    valid = (np.isfinite(X) | ~present[:, None, :]).all(axis=2) & np.isfinite(Y)
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

    def round_weights(w, on):
        """The play w spread over the experts in the round: the others' share is dropped."""
        if w[on].sum() > 0:
            return w[on] / w[on].sum()
        return np.full(on.sum(), 1 / on.sum())

    def rmse(w, Xc, yc, ac, on):
        return float(np.sqrt(np.sum(ac * (Xc @ round_weights(w, on) - yc) ** 2)))

    def rmse_grad(w, Xc, yc, ac, on):
        wp = round_weights(w, on)
        err = Xc @ wp - yc
        loss = np.sqrt(np.sum(ac * err**2))
        g = np.zeros(n_model)
        if np.isclose(loss, 0.0):
            return g
        g[on] = Xc.T @ (ac * err) / loss
        # An expert sitting the round out is scored as the blend of those in it, so it
        # gains or loses no regret (sleeping experts).
        g[~on] = wp @ g[on]
        return g

    # Loss of each scored round as a function of the play w, as poold expects.
    losses = {}
    for t in np.flatnonzero(scored):
        cells, on = valid[t], present[t]
        args = {
            "Xc": X[t][np.ix_(cells, on)],
            "yc": Y[t, cells],
            "ac": area[cells] / area[cells].sum(),
            "on": on,
        }
        losses[round_no[t]] = {
            "fun": lambda w, a=args: rmse(w, **a),
            "grad": lambda w, a=args: rmse_grad(w, **a),
        }

    with warnings.catch_warnings():
        # poold's plotting module has invalid escape sequences in its strings.
        warnings.simplefilter("ignore", SyntaxWarning)
        import poold
        from poold.utils import loss_regret
    ol = poold.create(learner, model_list=models)
    # Observed gradients the hints are built from.
    g_last, g_sum, g_n = np.zeros(n_model), np.zeros(n_model), 0
    if state:
        for k, v in state["poold"]["learner"].items():
            cur = getattr(ol, k)
            if isinstance(cur, np.ndarray):
                v = np.array(v)
            elif isinstance(cur, set):
                v = set(v)
            setattr(ol, k, v)
        hist = state["poold"]["history"]
        for name in ("play_history", "hint_history", "grad_history"):
            getattr(ol.history, name).update({int(s): np.array(v) for s, v in hist[name].items()})
        ol.history.os_history.update({int(s): set(v) for s, v in hist["os_history"].items()})
        ol.history.param_history.update({int(s): v for s, v in hist["param_history"].items()})
        g_last = np.array(state["hint_grads"]["last"])
        g_sum = np.array(state["hint_grads"]["sum"])
        g_n = state["hint_grads"]["count"]
        joined = [models.index(m) for m in state["joined"]]
    else:
        joined = []
    # DORM and DORM+ play in regret space, so their hints are regrets; AdaHedgeD and
    # DUB take gradient hints.
    regret_hint = learner in ("dorm", "dormplus")
    with warnings.catch_warnings():
        # AdaHedgeD divides by a zero hint error when a hint is exact.
        warnings.simplefilter("ignore", RuntimeWarning)
        local = {t: j for j, t in enumerate(round_no)}
        for j, t in enumerate(round_no):
            if t < ol.t:
                continue  # played in an earlier run
            # An expert's first round: start it at the mean weight of those already in,
            # by setting its learner state to their mean.
            new = [i for i in np.flatnonzero(present[j]) if i not in joined]
            if joined and new:
                if learner == "dormplus":
                    ol.p[new] = ol.p[joined].mean()
                elif learner == "dorm":
                    ol.regret[new] = np.maximum(0.0, ol.regret[joined]).mean()
                elif np.isclose(ol.lam, 0.0):
                    ol.theta[new] = ol.theta[joined].min()
                else:
                    th = ol.theta[joined]
                    ol.theta[new] = th.min() - ol.lam * np.log(
                        np.mean(np.exp(-(th - th.min()) / ol.lam))
                    )
                print(f"Note: {[models[i] for i in new]} join at {labels[j]}.", file=sys.stderr)
            joined += [int(i) for i in new]
            os_times = sorted(ol.get_outstanding())
            losses_fb = [(s, losses[s]) for s in os_times if s + delay <= t and s in losses]
            ol.record_losses(losses_fb)
            for s, _ in losses_fb:
                g_last = ol.get_grad(s)
                g_sum = g_sum + g_last
                g_n += 1
            pending = [s for s in os_times if s not in dict(losses_fb)]
            h = None
            if hint != "none" and g_n:
                g_tilde = g_last if hint == "recent_g" else g_sum / g_n
                h_vec = np.zeros(n_model)
                for s in pending:
                    # Experts sitting round s out are hinted as the blend, as in its loss.
                    on, w_s = present[local[s]], ol.get_play(s)
                    g_s = g_tilde.copy()
                    g_s[~on] = round_weights(w_s, on) @ g_tilde[on]
                    if regret_hint:
                        h_vec += loss_regret(g_s, w_s, ol.groups)
                    else:
                        h_vec += g_s
                h = {"grad": lambda w, h_vec=h_vec: h_vec}
            ol.update_and_play(losses_fb, hint=h)

    weights = np.zeros((n_round, n_model))
    for j, t in enumerate(round_no):
        weights[j, present[j]] = round_weights(ol.history.play_history[t], present[j])
    expert_rmse = np.full((n_round, n_model), np.nan)
    ensemble_rmse = np.full(n_round, np.nan)
    for t in np.flatnonzero(scored):
        loss = losses[round_no[t]]
        ensemble_rmse[t] = loss["fun"](weights[t])
        for i in np.flatnonzero(present[t]):
            expert_rmse[t, i] = loss["fun"](np.eye(n_model)[i])

    if state_out:
        # Everything poold reads when a waiting round's obs arrive: the learner's own
        # state, and the history of the waiting rounds and the rounds outstanding with them.
        waiting = sorted(ol.outstanding)
        needed = set(waiting).union(*(ol.history.os_history[s] for s in waiting))
        hist = ol.history
        label = dict(zip(round_no, keys, strict=True))
        skip = ("history", "expert_models", "groups", "group_keys", "alg", "d", "reg", "T")
        new_state = {
            "learner": learner,
            "hint": hint,
            "delay": delay,
            "models": models,
            "joined": [models[i] for i in joined],
            "last_round": keys[-1],
            "waiting": {label[s]: s for s in waiting},
            "hint_grads": {"last": g_last.tolist(), "sum": g_sum.tolist(), "count": g_n},
            "poold": {
                "learner": {
                    k: sorted(v) if isinstance(v, set) else np.asarray(v).tolist()
                    for k, v in vars(ol).items()
                    if k not in skip
                },
                "history": {
                    "play_history": {
                        s: hist.play_history[s].tolist() for s in needed if s in hist.play_history
                    },
                    "hint_history": {
                        s: hist.hint_history[s].tolist() for s in needed if s in hist.hint_history
                    },
                    "grad_history": {
                        s: hist.grad_history[s].tolist() for s in needed if s in hist.grad_history
                    },
                    "os_history": {
                        s: sorted(hist.os_history[s]) for s in needed if s in hist.os_history
                    },
                    "param_history": {
                        s: {k: float(v) for k, v in hist.param_history[s].items()}
                        for s in needed
                        if s in hist.param_history
                    },
                },
            },
        }
        with open(state_out, "w") as f:
            json.dump(new_state, f)
        print(f"Wrote state: {state_out}", file=sys.stderr)

    weight = xr.DataArray(
        weights,
        dims=("time", model_dim),
        coords={"time": fc["time"], model_dim: fc[model_dim]},
        attrs={"long_name": "Online ensemble weight", "units": "1"},
    )
    # Experts sitting a round out carry zero weight there; their NaNs must not blank it.
    in_round = xr.DataArray(present, dims=("time", model_dim), coords=weight.coords)
    ens = (fc.where(in_round, 0.0) * weight).sum(model_dim, skipna=False)
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
    with warnings.catch_warnings():
        # An expert with no scored rounds yet has no mean (NaN).
        warnings.simplefilter("ignore", RuntimeWarning)
        mean_expert = np.nanmean(expert_rmse, axis=0)
        # Mean per scored round, against each expert over the rounds it took part in.
        regret = np.nanmin(np.nanmean(ensemble_rmse[:, None] - expert_rmse, axis=0))
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

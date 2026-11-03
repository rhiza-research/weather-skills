"""Correctness tests for online-ensemble."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def ensemble_fn():
    return load_skill("online-ensemble", "online_ensemble").online_ensemble


def _inputs(tmp_path, n_time=30, obs_time=None, seed=0):
    """Obs plus three experts: close to the obs, biased, and noisy."""
    rng = np.random.default_rng(seed)
    obs = make_gridded(n_time=n_time, start="2026-01-05")
    obs["precip"].values = rng.gamma(2.0, 2.0, obs["precip"].shape)
    obs["precip"].attrs["units"] = "mm day-1"
    truth = obs["precip"]
    experts = [
        truth + rng.normal(0, 0.2, truth.shape),
        truth + 3.0,
        truth + rng.normal(0, 3.0, truth.shape),
    ]
    fc = xr.concat(experts, dim="model").assign_coords(model=["good", "biased", "noisy"])
    fc.attrs["units"] = "mm day-1"
    fc_path = write_zarr(fc.to_dataset(name="precip"), tmp_path / "fc.zarr")
    if obs_time is not None:
        obs = obs.isel(time=slice(0, obs_time))
    obs_path = write_zarr(obs, tmp_path / "obs.zarr")
    return fc_path, obs_path


def _run(fn, fc, obs, out, *extra):
    run_skill(fn, "--forecast", str(fc), "--obs", str(obs), "-o", str(out), *extra)
    return xr.open_zarr(out, consolidated=True).load()


@pytest.mark.parametrize("learner", ["dormplus", "dorm", "adahedged", "dub"])
@pytest.mark.parametrize("hint", ["recent_g", "mean_g", "none"])
def test_good_expert_gains_weight(tmp_path, ensemble_fn, learner, hint):
    fc, obs = _inputs(tmp_path)
    ds = _run(
        ensemble_fn,
        fc,
        obs,
        tmp_path / "out.zarr",
        "--delay",
        "3",
        "--learner",
        learner,
        "--hint",
        hint,
    )

    w = ds["weight"]
    np.testing.assert_allclose(w.sum("model"), 1.0)
    # No feedback before round `delay`: uniform weights.
    np.testing.assert_allclose(w.isel(time=slice(0, 3)), 1 / 3)
    assert str(w.isel(time=-1).idxmax("model").values) == "good"
    late = slice(10, None)
    assert float(ds["ensemble_rmse"].isel(time=late).mean()) < float(
        ds["expert_rmse"].isel(time=late).sel(model="noisy").mean()
    )


def test_ensemble_is_weighted_sum(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    ds = _run(ensemble_fn, fc, obs, tmp_path / "out.zarr", "--delay", "2")

    experts = xr.open_zarr(fc, consolidated=True)["precip"].load()
    expected = (experts * ds["weight"]).sum("model")
    np.testing.assert_allclose(ds["precip"].values, expected.values, rtol=1e-6)
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["precip"].attrs["ensemble_method"] == "poold dormplus, hint recent_g, delay 2"
    assert "Weights for 2026-02-03" in ds.attrs["online_ensemble_summary"]
    assert load_history(tmp_path / "out.zarr")[-1]["skill"] == "online-ensemble"


def test_weights_never_see_future_obs(tmp_path, ensemble_fn):
    """Changing round k's obs changes no weight before round k + delay."""
    fc, obs = _inputs(tmp_path)
    base = _run(ensemble_fn, fc, obs, tmp_path / "a.zarr", "--delay", "3")

    changed = xr.open_zarr(obs, consolidated=True).load()
    changed["precip"][10] += 5.0
    obs2 = write_zarr(changed, tmp_path / "obs2.zarr")
    ds = _run(ensemble_fn, fc, obs2, tmp_path / "b.zarr", "--delay", "3")

    np.testing.assert_allclose(
        ds["weight"].isel(time=slice(0, 13)), base["weight"].isel(time=slice(0, 13))
    )
    assert not np.allclose(ds["weight"].isel(time=13), base["weight"].isel(time=13))


def test_live_rounds_after_last_obs(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path, n_time=20, obs_time=16)
    ds = _run(ensemble_fn, fc, obs, tmp_path / "out.zarr", "--delay", "4")

    assert ds.sizes["time"] == 20
    assert np.isnan(ds["ensemble_rmse"].isel(time=slice(16, None))).all()
    assert np.isfinite(ds["ensemble_rmse"].isel(time=slice(0, 16))).all()
    assert np.isfinite(ds["precip"]).all()
    assert "16 scored" in ds.attrs["online_ensemble_summary"]


def test_obs_converted_to_forecast_units(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    base = _run(ensemble_fn, fc, obs, tmp_path / "a.zarr", "--delay", "2")

    hourly = xr.open_zarr(obs, consolidated=True).load()
    hourly["precip"] = hourly["precip"] / 24
    hourly["precip"].attrs["units"] = "mm hr-1"
    obs2 = write_zarr(hourly, tmp_path / "obs_hr.zarr")
    ds = _run(ensemble_fn, fc, obs2, tmp_path / "b.zarr", "--delay", "2")

    np.testing.assert_allclose(ds["weight"], base["weight"], atol=1e-9)


def test_obs_gap_exits(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    gappy = xr.open_zarr(obs, consolidated=True).load()
    gappy["precip"][5] = np.nan
    obs2 = write_zarr(gappy, tmp_path / "obs_gap.zarr")
    with pytest.raises(SystemExit) as exc:
        _run(ensemble_fn, fc, obs2, tmp_path / "out.zarr", "--delay", "2")
    assert exc.value.code == 2


def test_needs_model_dim(tmp_path, ensemble_fn):
    _, obs = _inputs(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(ensemble_fn, obs, obs, tmp_path / "out.zarr", "--delay", "2")
    assert exc.value.code == 2


def test_delay_must_be_positive(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(ensemble_fn, fc, obs, tmp_path / "out.zarr", "--delay", "0")
    assert exc.value.code == 2


@pytest.mark.parametrize("learner", ["dormplus", "dorm", "adahedged", "dub"])
@pytest.mark.parametrize("hint", ["recent_g", "mean_g", "none"])
def test_resume_from_state_matches_one_run(tmp_path, ensemble_fn, learner, hint):
    fc, obs = _inputs(tmp_path)
    opts = ("--delay", "3", "--learner", learner, "--hint", hint)
    full = _run(ensemble_fn, fc, obs, tmp_path / "full.zarr", *opts)

    first = xr.open_zarr(fc, consolidated=True).isel(time=slice(0, 18))
    fc_first = write_zarr(first, tmp_path / "fc_first.zarr")
    state = tmp_path / "state.json"
    _run(ensemble_fn, fc_first, obs, tmp_path / "a.zarr", *opts, "--state-out", str(state))
    # The full archive again: finished rounds are skipped, waiting ones are scored.
    ds = _run(ensemble_fn, fc, obs, tmp_path / "b.zarr", *opts, "--state-in", str(state))

    assert ds.sizes["time"] == 15  # 3 rounds waiting at the split + 12 new
    np.testing.assert_allclose(
        ds["weight"].values, full["weight"].isel(time=slice(15, None)).values, atol=1e-12
    )
    np.testing.assert_allclose(
        ds["ensemble_rmse"].values, full["ensemble_rmse"].isel(time=slice(15, None)).values
    )


def test_state_chains_over_three_runs(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    full = _run(ensemble_fn, fc, obs, tmp_path / "full.zarr", "--delay", "3")

    fc_all = xr.open_zarr(fc, consolidated=True)
    state_in = []
    for i, end in enumerate((10, 20, 30)):
        part = write_zarr(fc_all.isel(time=slice(0, end)), tmp_path / f"fc{i}.zarr")
        ds = _run(
            ensemble_fn,
            part,
            obs,
            tmp_path / f"out{i}.zarr",
            "--delay",
            "3",
            *state_in,
            "--state-out",
            str(tmp_path / f"state{i}.json"),
        )
        state_in = ["--state-in", str(tmp_path / f"state{i}.json")]
    np.testing.assert_allclose(
        ds["weight"].values, full["weight"].isel(time=slice(17, None)).values, atol=1e-12
    )


def test_state_settings_must_match(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    state = tmp_path / "state.json"
    _run(ensemble_fn, fc, obs, tmp_path / "a.zarr", "--delay", "3", "--state-out", str(state))
    with pytest.raises(SystemExit) as exc:
        _run(
            ensemble_fn,
            fc,
            obs,
            tmp_path / "b.zarr",
            "--delay",
            "3",
            "--learner",
            "dorm",
            "--state-in",
            str(state),
        )
    assert exc.value.code == 2


def test_state_needs_waiting_rounds(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    state = tmp_path / "state.json"
    _run(ensemble_fn, fc, obs, tmp_path / "a.zarr", "--delay", "3", "--state-out", str(state))
    later = write_zarr(
        xr.open_zarr(fc, consolidated=True).isel(time=slice(29, None)), tmp_path / "late.zarr"
    )
    with pytest.raises(SystemExit) as exc:
        _run(ensemble_fn, later, obs, tmp_path / "b.zarr", "--delay", "3", "--state-in", str(state))
    assert exc.value.code == 2


def _late_good(tmp_path, join=10):
    """The good expert has no forecasts before round `join`."""
    fc, obs = _inputs(tmp_path)
    ds = xr.open_zarr(fc, consolidated=True).load()
    ds["precip"].loc[{"model": "good"}] = (
        ds["precip"].sel(model="good").where(ds["time"] >= ds["time"][join])
    )
    return write_zarr(ds, tmp_path / "fc_late.zarr"), obs


@pytest.mark.parametrize("learner", ["dormplus", "dorm", "adahedged", "dub"])
def test_expert_joins_when_it_comes_online(tmp_path, ensemble_fn, learner):
    fc, obs = _late_good(tmp_path)
    ds = _run(ensemble_fn, fc, obs, tmp_path / "out.zarr", "--delay", "3", "--learner", learner)

    w = ds["weight"]
    np.testing.assert_allclose(w.sum("model"), 1.0)
    np.testing.assert_array_equal(w.sel(model="good").isel(time=slice(0, 10)), 0.0)
    # It joins near the mean weight of the experts already in, not at zero or all.
    assert 0.15 < float(w.sel(model="good").isel(time=10)) < 0.6
    assert str(w.isel(time=-1).idxmax("model").values) == "good"
    assert np.isfinite(ds["precip"]).all()
    assert np.isnan(ds["expert_rmse"].sel(model="good").isel(time=slice(0, 10))).all()
    assert np.isfinite(ds["expert_rmse"].sel(model="good").isel(time=slice(10, None))).all()


@pytest.mark.parametrize("learner", ["dormplus", "dorm"])
@pytest.mark.parametrize("hint", ["recent_g", "mean_g", "none"])
def test_weights_before_join_ignore_the_absent_expert(tmp_path, ensemble_fn, learner, hint):
    """Until it joins, regret matching weighs the others as if it were not in the stack."""
    fc, obs = _late_good(tmp_path)
    opts = ("--delay", "3", "--learner", learner, "--hint", hint)
    ds = _run(ensemble_fn, fc, obs, tmp_path / "out.zarr", *opts)

    two = xr.open_zarr(fc, consolidated=True).sel(model=["biased", "noisy"])
    fc_two = write_zarr(two, tmp_path / "fc_two.zarr")
    alone = _run(ensemble_fn, fc_two, obs, tmp_path / "two.zarr", *opts)
    np.testing.assert_allclose(
        ds["weight"].sel(model=["biased", "noisy"]).isel(time=slice(0, 10)),
        alone["weight"].isel(time=slice(0, 10)),
        atol=1e-12,
    )


@pytest.mark.parametrize("learner", ["dormplus", "dorm", "adahedged", "dub"])
@pytest.mark.parametrize("split", [8, 12])
def test_resume_across_a_join(tmp_path, ensemble_fn, learner, split):
    fc, obs = _late_good(tmp_path)
    opts = ("--delay", "3", "--learner", learner)
    full = _run(ensemble_fn, fc, obs, tmp_path / "full.zarr", *opts)

    first = write_zarr(
        xr.open_zarr(fc, consolidated=True).isel(time=slice(0, split)), tmp_path / "first.zarr"
    )
    state = tmp_path / "state.json"
    _run(ensemble_fn, first, obs, tmp_path / "a.zarr", *opts, "--state-out", str(state))
    ds = _run(ensemble_fn, fc, obs, tmp_path / "b.zarr", *opts, "--state-in", str(state))
    np.testing.assert_allclose(
        ds["weight"].values, full["weight"].isel(time=slice(split - 3, None)).values, atol=1e-12
    )


def test_regret_is_against_the_best_expert(tmp_path, ensemble_fn):
    fc, obs = _inputs(tmp_path)
    ds = _run(ensemble_fn, fc, obs, tmp_path / "out.zarr", "--delay", "3")

    best = float((ds["ensemble_rmse"] - ds["expert_rmse"]).mean("time").max())
    assert f"mean regret vs best expert {best:+.3g}" in ds.attrs["online_ensemble_summary"]
    # The blend trails the near-perfect expert, so regret against the best is positive.
    assert best > 0

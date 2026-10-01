"""Correctness tests for verify."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def verify_fn():
    return load_skill("verify", "verify").verify


def _grid(*, event_at, fill=0.0, name="precip"):
    ds = make_gridded(n_time=1, fill=fill, name=name)
    for i, j in event_at:
        ds[name].values[0, i, j] = 5.0
    return ds


def test_hit_disagree_and_below(tmp_path, verify_fn):
    fc = write_zarr(_grid(event_at=[(0, 0), (0, 1)]), tmp_path / "fc.zarr")
    obs = write_zarr(_grid(event_at=[(0, 0), (1, 0)]), tmp_path / "obs.zarr")
    out = tmp_path / "hits.zarr"

    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--variable",
        "precip",
        "--threshold",
        "1",
        "-o",
        str(out),
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert Path(out).exists()
    hit = ds["event_hit"].isel(time=0).values
    assert hit[0, 0] == pytest.approx(1)
    assert hit[0, 1] == pytest.approx(-1)
    assert hit[1, 0] == pytest.approx(-1)
    assert hit[1, 1] == pytest.approx(0)
    assert ds["event_hit"].attrs["event_threshold"] == 1.0
    assert ds["event_hit"].attrs["event_variable"] == "precip"
    assert ds.attrs["verify_metric"] == "hits"
    assert "hit rate 50%" in ds.attrs.get("verify_score_summary", "")
    assert load_history(out)[-1]["skill"] == "verify"


def test_threshold_raises_the_bar(tmp_path, verify_fn):
    ds = make_gridded(n_time=1, fill=2.0)
    fc = write_zarr(ds, tmp_path / "fc.zarr")
    obs = write_zarr(ds.copy(), tmp_path / "obs.zarr")
    out = tmp_path / "hits.zarr"

    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--threshold",
        "10",
        "-o",
        str(out),
    )
    hit = xr.open_zarr(out, consolidated=True)["event_hit"].values
    np.testing.assert_array_equal(hit, 0)


def test_different_variable_names(tmp_path, verify_fn):
    fc = write_zarr(make_gridded(n_time=1, fill=5.0, name="tp"), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=1, fill=5.0, name="precip"), tmp_path / "obs.zarr")
    out = tmp_path / "hits.zarr"
    run_skill(verify_fn, "--forecast", str(fc), "--obs", str(obs), "-o", str(out))
    assert xr.open_zarr(out, consolidated=True)["event_hit"].values == pytest.approx(1)


def test_bias_writes_field(tmp_path, verify_fn, capsys):
    fc = write_zarr(make_gridded(n_time=1, fill=3.0), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=1, fill=1.0), tmp_path / "obs.zarr")
    out = tmp_path / "bias.zarr"
    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--metric",
        "bias",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert "bias" in ds
    assert ds.attrs["verify_metric"] == "bias"
    assert ds["bias"].values == pytest.approx(2.0)
    assert "bias" in capsys.readouterr().out


def test_mae_writes_field(tmp_path, verify_fn, capsys):
    fc = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=1, fill=2.0), tmp_path / "obs.zarr")
    out = tmp_path / "mae.zarr"
    run_skill(
        verify_fn,
        "--forecast",
        str(fc),
        "--obs",
        str(obs),
        "--metric",
        "mae",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert "mae" in ds
    assert "MAE" in capsys.readouterr().out


def test_step_forecast_without_time_is_refused(tmp_path, verify_fn):
    fc = write_zarr(make_forecast(n_step=2, fill=5.0), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=2, fill=5.0), tmp_path / "obs.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            verify_fn,
            "--forecast",
            str(fc),
            "--obs",
            str(obs),
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_obs_finer_grid_is_refused(tmp_path, verify_fn):
    fc = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 2.0), lons=(10.0, 11.0)),
        tmp_path / "fc.zarr",
    )
    obs = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0)),
        tmp_path / "obs.zarr",
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(
            verify_fn,
            "--forecast",
            str(fc),
            "--obs",
            str(obs),
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


# --- probabilistic / reduced metrics -------------------------------------------------------


def _ens(values, name="precip"):
    """Ensemble on the make_gridded grid: values has shape (member, time, lat, lon)."""
    base = make_gridded(n_time=values.shape[1], name=name)
    da = xr.DataArray(
        np.asarray(values, dtype=float),
        dims=("number", "time", "latitude", "longitude"),
        coords={"number": np.arange(values.shape[0]), **{k: base[k] for k in base[name].dims}},
        attrs=base[name].attrs,
    )
    return da.to_dataset(name=name)


def _obs(values, name="precip"):
    ds = make_gridded(n_time=values.shape[0], name=name)
    ds[name].values[:] = values
    return ds


def _run(tmp_path, verify_fn, fc, obs, *extra):
    f = write_zarr(fc, tmp_path / "fc.zarr")
    o = write_zarr(obs, tmp_path / "obs.zarr")
    out = tmp_path / "v.zarr"
    run_skill(verify_fn, "--forecast", str(f), "--obs", str(o), "-o", str(out), *extra)
    return xr.open_zarr(out, consolidated=True)


RNG = np.random.default_rng(7)
SHAPE = (5, 4, 3, 4)  # member, time, lat, lon


def _pair_sum(x):
    return np.abs(x[:, None] - x[None, :]).sum((0, 1))  # sum_{i,j} |x_i - x_j|, M^2 terms


def test_crps_fair_matches_the_weatherbench2_estimator(tmp_path, verify_fn):
    """Fair estimator (opt-in): spread term divided by M(M-1) (WeatherBench 2 CRPSSpread)."""
    x = RNG.gamma(2.0, 3.0, SHAPE)
    y = RNG.gamma(2.0, 3.0, SHAPE[1:])
    m = SHAPE[0]
    ds = _run(tmp_path, verify_fn, _ens(x), _obs(y), "--metric", "crps", "--crps-estimator", "fair")
    expected = np.abs(x - y).mean(0) - _pair_sum(x) / (2 * m * (m - 1))
    np.testing.assert_allclose(ds["crps"].values, expected, rtol=1e-5, atol=1e-6)
    assert ds["crps"].attrs["verify_crps_estimator"] == "fair"
    assert ds["crps"].attrs["verify_ensemble_size"] == m
    assert "verify_ensemble_reduction" not in ds["crps"].attrs  # members were NOT averaged


def test_crps_standard_matches_the_pairwise_formula(tmp_path, verify_fn):
    x = RNG.gamma(2.0, 3.0, SHAPE)
    y = RNG.gamma(2.0, 3.0, SHAPE[1:])
    m = SHAPE[0]
    ds = _run(
        tmp_path, verify_fn, _ens(x), _obs(y), "--metric", "crps", "--crps-estimator", "standard"
    )
    expected = np.abs(x - y).mean(0) - _pair_sum(x) / (2 * m * m)
    np.testing.assert_allclose(ds["crps"].values, expected, rtol=1e-5, atol=1e-6)
    assert (ds["crps"].values >= -1e-6).all()  # the M^2 estimator is a proper, non-negative score


def test_crps_standard_agrees_with_xskillscore_when_available(tmp_path, verify_fn):
    xs = pytest.importorskip("xskillscore")
    x = RNG.normal(10, 2, SHAPE)
    y = RNG.normal(10, 2, SHAPE[1:])
    ds = _run(
        tmp_path, verify_fn, _ens(x), _obs(y), "--metric", "crps", "--crps-estimator", "standard"
    )
    ref = xs.crps_ensemble(_obs(y)["precip"], _ens(x)["precip"], member_dim="number", dim=[])
    np.testing.assert_allclose(ds["crps"].values, ref.values, rtol=1e-5, atol=1e-6)


def test_crps_of_one_member_is_mae_and_spread_helps(tmp_path, verify_fn):
    y = np.full(SHAPE[1:], 5.0)
    one = _run(
        tmp_path,
        verify_fn,
        _ens(np.full((1, *SHAPE[1:]), 7.0)),
        _obs(y),
        "--metric",
        "crps",
        "--crps-estimator",
        "standard",
    )
    np.testing.assert_allclose(one["crps"].values, 2.0, rtol=1e-6)
    spread = np.stack([np.full(SHAPE[1:], v) for v in (6.0, 8.0)])  # same mean 7, has spread
    two = _run(tmp_path / "b", verify_fn, _ens(spread), _obs(y), "--metric", "crps")
    assert (two["crps"].values < one["crps"].values).all()


def test_fair_crps_refuses_a_single_member(tmp_path, verify_fn):
    with pytest.raises((Exception, SystemExit)):
        _run(tmp_path, verify_fn, _ens(np.ones((1, *SHAPE[1:]))), _obs(np.ones(SHAPE[1:])),
             "--metric", "crps", "--crps-estimator", "fair")  # fmt: skip


def test_crps_is_nan_where_obs_is_missing(tmp_path, verify_fn):
    y = RNG.normal(0, 1, SHAPE[1:])
    y[0, 0, 0] = np.nan
    ds = _run(tmp_path, verify_fn, _ens(RNG.normal(0, 1, SHAPE)), _obs(y), "--metric", "crps")
    assert np.isnan(ds["crps"].values[0, 0, 0])
    assert np.isfinite(ds["crps"].values[1:]).all()


def test_reduce_over_the_ensemble_dim_is_refused(tmp_path, verify_fn):
    with pytest.raises((Exception, SystemExit)):
        _run(tmp_path, verify_fn, _ens(np.ones(SHAPE)), _obs(np.ones(SHAPE[1:])),
             "--metric", "crps", "--reduce", "number")  # fmt: skip


def test_crps_is_exact_for_a_large_ensemble(tmp_path, verify_fn):
    """51 members on a 20x20 grid via the sorted-member identity; the old pairwise form built
    51*51 grid copies. Values must match the pairwise definition exactly."""
    lats, lons = tuple(float(v) for v in range(20)), tuple(float(v) for v in range(10, 30))
    base = make_gridded(n_time=1, lats=lats, lons=lons)
    x = RNG.normal(0, 1, (51, 1, 20, 20))
    y = RNG.normal(0, 1, (1, 20, 20))
    fc = xr.DataArray(
        x,
        dims=("number", "time", "latitude", "longitude"),
        coords={"number": np.arange(51), **{k: base[k] for k in ("time", "latitude", "longitude")}},
        attrs=base["precip"].attrs,
    ).to_dataset(name="precip")
    ob = base.copy(deep=True)
    ob["precip"].values[:] = y
    ds = _run(tmp_path, verify_fn, fc, ob, "--metric", "crps", "--crps-estimator", "fair")
    expected = np.abs(x - y).mean(0) - _pair_sum(x) / (2 * 51 * 50)
    np.testing.assert_allclose(ds["crps"].values, expected, rtol=1e-5, atol=1e-6)


def test_brier_is_exact(tmp_path, verify_fn):
    x = np.zeros(SHAPE)
    x[:2] = 10.0  # 2 of 5 members exceed -> p = 0.4
    y = np.zeros(SHAPE[1:])
    y[0] = 10.0  # event observed at time 0 only
    ds = _run(tmp_path, verify_fn, _ens(x), _obs(y), "--metric", "brier", "--threshold", "5")
    bs = ds["brier_score"].values
    np.testing.assert_allclose(bs[0], (0.4 - 1) ** 2, rtol=1e-6)
    np.testing.assert_allclose(bs[1:], 0.4**2, rtol=1e-6)
    assert ((bs >= 0) & (bs <= 1)).all()


def test_rmse_reduces_over_time(tmp_path, verify_fn):
    x = RNG.normal(0, 1, SHAPE[1:])
    y = RNG.normal(0, 1, SHAPE[1:])
    ds = _run(tmp_path, verify_fn, _obs(x), _obs(y), "--metric", "rmse", "--reduce", "time")
    np.testing.assert_allclose(ds["rmse"].values, np.sqrt(((x - y) ** 2).mean(0)), rtol=1e-5)
    assert "time" not in ds["rmse"].dims
    assert ds["rmse"].attrs["verify_reduce_dims"] == "time"


@pytest.mark.parametrize(
    ("metric", "extra"),
    [("crps", []), ("brier", ["--threshold", "1"]), ("rmse", [])],
)
def test_invalid_metric_data_combinations_are_refused(tmp_path, verify_fn, metric, extra):
    det = _obs(np.ones(SHAPE[1:]))
    with pytest.raises((Exception, SystemExit)):
        _run(tmp_path, verify_fn, det, det, "--metric", metric, *extra)


def test_brier_without_threshold_is_refused(tmp_path, verify_fn):
    with pytest.raises((Exception, SystemExit)):
        _run(
            tmp_path, verify_fn, _ens(np.ones(SHAPE)), _obs(np.ones(SHAPE[1:])), "--metric", "brier"
        )


def test_deterministic_metric_records_ensemble_averaging(tmp_path, verify_fn):
    ds = _run(
        tmp_path, verify_fn, _ens(np.ones(SHAPE)), _obs(np.ones(SHAPE[1:])), "--metric", "mae"
    )
    assert ds["mae"].attrs["verify_ensemble_reduction"] == "mean over number before scoring"


def test_verifier_rejects_a_perturbed_crps(tmp_path, verify_fn):
    """Mutation check: the reference comparison above must fail on a plausible wrong answer."""
    x = RNG.gamma(2.0, 3.0, SHAPE)
    y = RNG.gamma(2.0, 3.0, SHAPE[1:])
    ds = _run(tmp_path, verify_fn, _ens(x), _obs(y), "--metric", "crps")
    mae_of_mean = np.abs(x.mean(0) - y)  # the silent member-averaging mistake
    with pytest.raises(AssertionError):
        np.testing.assert_allclose(ds["crps"].values, mae_of_mean, rtol=1e-5, atol=1e-6)


def test_deterministic_metrics_score_the_ensemble_mean(tmp_path, verify_fn):
    """Members 3 and 7 around obs 5: the ensemble mean is exact (MAE 0) while the mean member
    error is 2. verify scores the mean, and now says so in verify_ensemble_reduction."""
    obs = _obs(np.full(SHAPE[1:], 5.0))
    ens = _ens(np.stack([np.full(SHAPE[1:], v) for v in (3.0, 7.0)]))
    ds = _run(tmp_path, verify_fn, ens, obs, "--metric", "mae")
    np.testing.assert_allclose(ds["mae"].values, 0.0)
    assert ds["mae"].attrs["verify_ensemble_reduction"].startswith("mean over number")


def test_crps_defaults_to_the_standard_estimator(tmp_path, verify_fn):
    """Default matches properscoring.crps_ensemble, which Sheerwater's benchmark uses."""
    x = RNG.gamma(2.0, 3.0, SHAPE)
    y = RNG.gamma(2.0, 3.0, SHAPE[1:])
    m = SHAPE[0]
    ds = _run(tmp_path, verify_fn, _ens(x), _obs(y), "--metric", "crps")
    np.testing.assert_allclose(
        ds["crps"].values, np.abs(x - y).mean(0) - _pair_sum(x) / (2 * m * m), rtol=1e-5, atol=1e-6
    )
    assert ds["crps"].attrs["verify_crps_estimator"] == "standard"


def test_brier_event_is_strictly_above_the_threshold(tmp_path, verify_fn):
    """A value exactly at the threshold is not an event (Sheerwater above_threshold,
    WeatherBench 2). Members at exactly 5 give p = 0; obs at exactly 5 is a non-event."""
    x = np.full(SHAPE, 5.0)
    y = np.full(SHAPE[1:], 5.0)
    ds = _run(tmp_path, verify_fn, _ens(x), _obs(y), "--metric", "brier", "--threshold", "5")
    np.testing.assert_allclose(ds["brier_score"].values, 0.0)

"""Correctness tests for verify."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
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


def _temp(value, units):
    ds = make_gridded(n_time=1, fill=value, name="t2m")
    ds["t2m"].attrs = {"units": units, "standard_name": "air_temperature"}
    return ds


def _verify(tmp_path, verify_fn, fc, obs, *args):
    pf = write_zarr(fc, tmp_path / "fc.zarr")
    po = write_zarr(obs, tmp_path / "obs.zarr")
    out = tmp_path / "out.zarr"
    run_skill(verify_fn, "--forecast", str(pf), "--obs", str(po), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True)


def test_obs_converted_to_forecast_units(tmp_path, verify_fn):
    """deg C forecast vs K obs: bias +2, not -271; units stay on the error field."""
    ds = _verify(
        tmp_path, verify_fn, _temp(27.0, "degree_Celsius"), _temp(298.15, "K"), "--metric", "bias"
    )
    assert ds["bias"].values == pytest.approx(2.0)
    assert ds["bias"].attrs["units"] == "delta_degC"

    rate = make_gridded(n_time=1, fill=24.0)
    hourly = make_gridded(n_time=1, fill=0.5)
    hourly["precip"].attrs["units"] = "mm hr-1"
    ds = _verify(tmp_path, verify_fn, rate, hourly, "--metric", "mae")
    assert ds["mae"].values == pytest.approx(12.0)
    assert ds["mae"].attrs["units"] == "mm day-1"


def test_threshold_applies_in_forecast_units(tmp_path, verify_fn):
    """36 deg C forecast vs 300 K (26.85 deg C) obs at 35: a miss, not a hit."""
    ds = _verify(
        tmp_path,
        verify_fn,
        _temp(36.0, "degree_Celsius"),
        _temp(300.0, "K"),
        "--metric",
        "hits",
        "--threshold",
        "35",
    )
    assert (ds["event_hit"].values == -1).all()


def test_total_against_rate_is_refused(tmp_path, verify_fn):
    total = make_gridded(n_time=1, fill=14.0)
    total["precip"].attrs = {"units": "mm"}
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, total, make_gridded(n_time=1, fill=2.0), "--metric", "mae")
    assert exc.value.code == 2


def test_station_bias_scores_and_keeps_units(tmp_path, verify_fn, capsys):
    """point_obs pairs (after point-value) get a plain-mean score and keep units."""
    fc = write_zarr(make_point_obs(fill=3.0), tmp_path / "fc.zarr")
    obs = write_zarr(make_point_obs(fill=1.0), tmp_path / "obs.zarr")
    out = tmp_path / "bias.zarr"

    run_skill(
        verify_fn, "--forecast", str(fc), "--obs", str(obs), "--metric", "bias", "-o", str(out)
    )

    assert "bias +2 mm / d  (mean of 6 values)" in capsys.readouterr().out
    bias = xr.open_zarr(out)["bias"]
    assert bias.attrs["units"] == "mm day-1"
    assert float(bias.mean()) == pytest.approx(2.0)


def test_no_overlap_error_names_the_dim(tmp_path, verify_fn, capsys):
    fc = write_zarr(make_gridded(n_time=2, start="2026-01-02"), tmp_path / "fc.zarr")
    obs = write_zarr(make_gridded(n_time=1, start="2026-01-01"), tmp_path / "obs.zarr")

    with pytest.raises(SystemExit) as exc:
        run_skill(
            verify_fn, "--forecast", str(fc), "--obs", str(obs), "--metric", "bias",
            "-o", str(tmp_path / "out.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no overlapping time labels" in err
    assert "2026-01-02" in err and "2026-01-01" in err


def test_multi_init_step_cube_is_refused(tmp_path, verify_fn):
    """A time (init) dim plus a step dim must not align the init with obs time."""
    fc = make_forecast(n_step=2, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc = fc.expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=2, lats=(1.0, 2.0), lons=(10.0, 11.0))
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, fc, obs, "--metric", "bias")
    assert exc.value.code == 2


def test_scalar_times_must_match(tmp_path, verify_fn, capsys):
    """Single-time inputs (time selected to a scalar) on different dates are refused."""
    fc = make_gridded(n_time=1, fill=3.0).isel(time=0)
    obs = make_gridded(n_time=1, fill=1.0, start="2026-02-01").isel(time=0)
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, fc, obs, "--metric", "bias")
    assert exc.value.code == 2
    assert "no overlapping time labels" in capsys.readouterr().err

    ds = _verify(tmp_path, verify_fn, fc, obs.assign_coords(time=fc.time), "--metric", "bias")
    assert ds["bias"].values == pytest.approx(2.0)


def test_units_on_one_side_only_is_refused(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, fill=3.0, name="x")
    fc["x"].attrs = {"units": "K"}
    obs = make_gridded(n_time=1, fill=1.0, name="x")
    obs["x"].attrs = {}
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, fc, obs, "--metric", "bias")
    assert exc.value.code == 2


def test_ensemble_mean_and_nan_member(tmp_path, verify_fn):
    """Members are averaged before scoring; a NaN member leaves that cell NaN."""
    fc = make_forecast(n_step=1, members=3, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[:] = np.array([0.0, 0.0, 3.0])[:, None, None, None]
    fc["precip"].values[2, 0, 0, 0] = np.nan
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=0.0)
    mae = _verify(tmp_path, verify_fn, fc, obs, "--metric", "mae")["mae"].isel(time=0).values
    assert np.isnan(mae[0, 0])
    np.testing.assert_allclose(mae.ravel()[1:], 1.0)


def test_selected_weeks_that_differ_are_refused(tmp_path, verify_fn, capsys):
    """select keeps the picked week, so mismatched weeks no longer score silently."""
    select = load_skill("select", "select_dim").select
    paths = {}
    for role, week in (("fc", "2026-09-28"), ("obs", "2026-09-14")):
        src = write_zarr(make_gridded(n_time=1, start=week), tmp_path / f"{role}_all.zarr")
        paths[role] = tmp_path / f"{role}.zarr"
        run_skill(select, "-i", str(src), "-o", str(paths[role]), "--dim", "time", "--index", "0")

    with pytest.raises(SystemExit) as exc:
        run_skill(
            verify_fn,
            "--forecast",
            str(paths["fc"]),
            "--obs",
            str(paths["obs"]),
            "--metric",
            "bias",
            "-o",
            str(tmp_path / "bias.zarr"),
        )
    assert exc.value.code == 2
    assert "no overlapping time" in capsys.readouterr().err


def _quintile_forecast(probs, edges=(1.0, 2.0, 3.0, 4.0)):
    """One-cell, one-week quintile forecast as pbc-correct writes it."""
    dims = ("time", "quintile", "latitude", "longitude")
    ds = xr.Dataset(
        {"tp": (dims, np.array(probs, dtype=float).reshape(1, 5, 1, 1))},
        coords={
            "time": [np.datetime64("2026-06-15", "ns")],
            "quintile": [0.2, 0.4, 0.6, 0.8, 1.0],
            "latitude": [1.0],
            "longitude": [10.0],
            "quintile_threshold": (dims, np.array([*edges, np.inf]).reshape(1, 5, 1, 1)),
        },
    )
    ds["tp"].attrs["units"] = "1"
    ds["quintile_threshold"].attrs["units"] = "mm day-1"
    return ds


def _weekly_obs(value):
    obs = make_gridded(n_time=1, lats=(1.0,), lons=(10.0,), fill=value, start="2026-06-15")
    return obs


def test_rps_perfect_and_climatology(tmp_path, verify_fn):
    # 2.5 mm day-1 falls in the third quintile (edges 2 and 3).
    perfect = _verify(
        tmp_path,
        verify_fn,
        _quintile_forecast([0, 0, 1, 0, 0]),
        _weekly_obs(2.5),
        "--metric",
        "rps",
    )
    assert float(perfect["rps"].values.squeeze()) == pytest.approx(0.0)
    assert "RPSS +1.000" in perfect.attrs["verify_score_summary"]

    clim = _verify(
        tmp_path, verify_fn, _quintile_forecast([0.2] * 5), _weekly_obs(2.5), "--metric", "rps"
    )
    # CDF 0.2, 0.4, 0.6, 0.8 against observed 0, 0, 1, 1.
    assert float(clim["rps"].values.squeeze()) == pytest.approx(0.04 + 0.16 + 0.16 + 0.04)
    assert "RPSS +0.000" in clim.attrs["verify_score_summary"]


def test_rps_converts_obs_to_edge_units(tmp_path, verify_fn):
    obs = _weekly_obs(17.5)  # a 7-day total equal to 2.5 mm day-1
    obs["precip"].attrs["units"] = "mm week-1"
    ds = _verify(tmp_path, verify_fn, _quintile_forecast([0, 0, 1, 0, 0]), obs, "--metric", "rps")
    assert float(ds["rps"].values.squeeze()) == pytest.approx(0.0)


def test_rps_needs_quintile_edges(tmp_path, verify_fn):
    fc = _quintile_forecast([0.2] * 5).drop_vars("quintile_threshold")
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, fc, _weekly_obs(2.5), "--metric", "rps")
    assert exc.value.code == 2


def _members(values, lats=(1.0,), lons=(10.0, 11.0)):
    """Ensemble on one day: values is (members, len(lats), len(lons))."""
    values = np.asarray(values, dtype=float)
    fc = make_forecast(n_step=1, members=values.shape[0], lats=lats, lons=lons, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    fc["precip"].values[:] = values[None]
    return fc


def _day_obs(values, lats=(1.0,), lons=(10.0, 11.0)):
    obs = make_gridded(n_time=1, lats=lats, lons=lons)
    obs["precip"].values[0] = np.asarray(values, dtype=float)
    return obs


def test_brier_uses_member_fraction(tmp_path, verify_fn):
    # Cell 1: half the members reach 1 mm and it rained -> (0.5 - 1)^2.
    # Cell 2: no member reaches it and it stayed dry -> 0.
    fc = _members([[[0.0, 0.0]], [[0.0, 0.0]], [[5.0, 0.0]], [[5.0, 0.0]]])
    ds = _verify(tmp_path, verify_fn, fc, _day_obs([[5.0, 0.0]]), "--metric", "brier")
    np.testing.assert_allclose(ds["brier"].isel(time=0).values, [[0.25, 0.0]])
    assert ds["brier"].attrs["event_threshold"] == 1.0
    # Base rate 50% -> reference 0.25; mean Brier 0.125 -> BSS +0.5.
    assert "BSS +0.500 vs base rate 50%" in ds.attrs["verify_score_summary"]


def test_brier_of_a_single_forecast(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, lats=(1.0,), lons=(10.0, 11.0), fill=5.0)
    ds = _verify(tmp_path, verify_fn, fc, _day_obs([[5.0, 0.0]]), "--metric", "brier")
    np.testing.assert_allclose(ds["brier"].isel(time=0).values, [[0.0, 1.0]])


def test_crps_matches_the_pairwise_definition(tmp_path, verify_fn):
    rng = np.random.default_rng(0)
    members = rng.gamma(2.0, 2.0, (11, 1, 2))
    obs = np.array([[3.0, 0.5]])
    ds = _verify(tmp_path, verify_fn, _members(members), _day_obs(obs), "--metric", "crps")

    expected = np.abs(members - obs).mean(axis=0) - 0.5 * np.abs(
        members[:, None] - members[None]
    ).mean(axis=(0, 1))
    np.testing.assert_allclose(ds["crps"].isel(time=0).values, expected, rtol=1e-5)
    assert ds["crps"].attrs["units"] == "mm day-1"
    assert ds.attrs["verify_score_summary"].startswith("CRPS ")


def test_crps_two_members(tmp_path, verify_fn):
    # Members 0 and 2 against 1: E|X - y| = 1, E|X - X'| = 1 -> 0.5.
    fc = _members([[[0.0, 0.0]], [[2.0, 0.0]]])
    ds = _verify(tmp_path, verify_fn, fc, _day_obs([[1.0, 0.0]]), "--metric", "crps")
    np.testing.assert_allclose(ds["crps"].isel(time=0).values, [[0.5, 0.0]])


def test_crps_fair_matches_the_pairwise_definition(tmp_path, verify_fn):
    # The fair score averages |X - X'| over the m(m-1) distinct pairs, not all m^2.
    rng = np.random.default_rng(1)
    members = rng.gamma(2.0, 2.0, (11, 1, 2))
    obs = np.array([[3.0, 0.5]])
    ds = _verify(tmp_path, verify_fn, _members(members), _day_obs(obs), "--metric", "crps_fair")

    m = members.shape[0]
    pair_sum = np.abs(members[:, None] - members[None]).sum(axis=(0, 1))
    expected = np.abs(members - obs).mean(axis=0) - 0.5 * pair_sum / (m * (m - 1))
    np.testing.assert_allclose(ds["crps_fair"].isel(time=0).values, expected, rtol=1e-5)
    assert ds["crps_fair"].attrs["units"] == "mm day-1"
    assert ds.attrs["verify_score_summary"].startswith("fair CRPS ")


def test_crps_fair_two_members(tmp_path, verify_fn):
    # Members 0 and 2 against 1: E|X - y| = 1, |X - X'| over the 2 distinct pairs = 2 -> 0.
    fc = _members([[[0.0, 0.0]], [[2.0, 0.0]]])
    ds = _verify(tmp_path, verify_fn, fc, _day_obs([[1.0, 0.0]]), "--metric", "crps_fair")
    np.testing.assert_allclose(ds["crps_fair"].isel(time=0).values, [[0.0, 0.0]], atol=1e-6)


def test_crps_fair_needs_two_members(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, lats=(1.0,), lons=(10.0, 11.0), fill=2.0)
    with pytest.raises(SystemExit):
        _verify(tmp_path, verify_fn, fc, _day_obs([[5.0, 0.0]]), "--metric", "crps_fair")


def test_crps_of_a_single_forecast_is_its_absolute_error(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, lats=(1.0,), lons=(10.0, 11.0), fill=2.0)
    ds = _verify(tmp_path, verify_fn, fc, _day_obs([[5.0, 0.0]]), "--metric", "crps")
    np.testing.assert_allclose(ds["crps"].isel(time=0).values, [[3.0, 2.0]])


def test_crps_nan_member_leaves_cell_nan(tmp_path, verify_fn):
    fc = _members([[[0.0, 0.0]], [[np.nan, 2.0]]])
    ds = _verify(tmp_path, verify_fn, fc, _day_obs([[1.0, 1.0]]), "--metric", "crps")
    crps = ds["crps"].isel(time=0).values
    assert np.isnan(crps[0, 0]) and np.isfinite(crps[0, 1])


def _series(values, members=False):
    """Forecast or obs over len(values) days on a 1 x len(cells) grid.

    values is (time, cells), or (time, members, cells) with members=True.
    """
    values = np.asarray(values, dtype=float)
    times = pd.date_range("2026-01-01", periods=values.shape[0], freq="D")
    dims = (
        ("time", "number", "latitude", "longitude")
        if members
        else ("time", "latitude", "longitude")
    )
    coords = {"time": times, "latitude": [1.0], "longitude": 10.0 + np.arange(values.shape[-1])}
    if members:
        coords["number"] = np.arange(values.shape[1])
    ds = xr.Dataset({"precip": (dims, values[..., None, :])}, coords=coords)
    ds["precip"].attrs["units"] = "mm day-1"
    return ds


def test_rmse_over_time(tmp_path, verify_fn):
    fc = _series([[1.0, 2.0], [3.0, 2.0]])
    obs = _series([[0.0, 2.0], [0.0, 2.0]])
    ds = _verify(tmp_path, verify_fn, fc, obs, "--metric", "rmse")
    assert "time" not in ds["rmse"].dims
    np.testing.assert_allclose(ds["rmse"].values, [[np.sqrt(5.0), 0.0]], rtol=1e-6)
    assert ds["rmse"].attrs["units"] == "mm day-1"
    assert ds.attrs["verify_score_summary"].startswith("RMSE 1.6")  # sqrt(10 / 4)


def test_spread_skill_of_a_calibrated_ensemble(tmp_path, verify_fn):
    # Two members at mean ± 1: corrected variance 2 * 3 / 2 = 3; the mean misses by sqrt(3).
    e = np.sqrt(3.0)
    fc = _series([[[e - 1], [e + 1]], [[-e - 1], [-e + 1]]], members=True)
    obs = _series([[0.0], [0.0]])
    ds = _verify(tmp_path, verify_fn, fc, obs, "--metric", "spread")
    np.testing.assert_allclose(ds["spread_skill"].values, [[1.0]], rtol=1e-5)
    assert "spread/skill 1.00" in ds.attrs["verify_score_summary"]


def test_spread_needs_members(tmp_path, verify_fn):
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, _series([[1.0]]), _series([[1.0]]), "--metric", "spread")
    assert exc.value.code == 2


def test_contingency_scores(tmp_path, verify_fn):
    # One hit, one false alarm, one miss, one correct negative.
    fc = _series([[5.0], [5.0], [0.0], [0.0]])
    obs = _series([[5.0], [0.0], [5.0], [0.0]])
    ds = _verify(tmp_path, verify_fn, fc, obs, "--metric", "contingency")
    for name, expected in (("pod", 0.5), ("far", 0.5), ("frequency_bias", 1.0), ("ets", 0.0)):
        np.testing.assert_allclose(ds[name].values, [[expected]], atol=1e-6)
    assert "1 hits, 1 false alarms, 1 misses" in ds.attrs["verify_score_summary"]


def test_correlation_over_time(tmp_path, verify_fn):
    obs = np.array([[1.0, 1.0], [3.0, 3.0], [2.0, 2.0], [5.0, 5.0]])
    fc = np.stack([2 * obs[:, 0] + 1, -obs[:, 1]], axis=1)
    ds = _verify(tmp_path, verify_fn, _series(fc), _series(obs), "--metric", "corr")
    np.testing.assert_allclose(ds["corr"].values, [[1.0, -1.0]], atol=1e-6)


def test_reliability_and_roc_of_a_perfect_forecast(tmp_path, verify_fn):
    obs = _series([[5.0, 0.0], [0.0, 5.0], [5.0, 5.0], [0.0, 0.0]])
    rel = _verify(tmp_path, verify_fn, obs, obs, "--metric", "reliability")
    assert rel.sizes["probability_bin"] == 10
    np.testing.assert_allclose(rel["observed_frequency"].sel(probability_bin=0.05), 0.0)
    np.testing.assert_allclose(rel["observed_frequency"].sel(probability_bin=0.95), 1.0)
    np.testing.assert_allclose(rel["sample_fraction"].sum(), 1.0)
    assert "reliability 0 " in rel.attrs["verify_score_summary"]

    roc = _verify(tmp_path, verify_fn, obs, obs, "--metric", "roc")
    assert roc.attrs["roc_area"] == pytest.approx(1.0)
    np.testing.assert_allclose(roc["false_alarm_rate"], 0.0)


def test_roc_of_an_inverted_forecast(tmp_path, verify_fn):
    obs = _series([[5.0, 0.0], [0.0, 5.0]])
    fc = _series([[0.0, 5.0], [5.0, 0.0]])
    roc = _verify(tmp_path, verify_fn, fc, obs, "--metric", "roc")
    assert roc.attrs["roc_area"] == pytest.approx(0.0)


def test_pooled_metrics_on_stations(tmp_path, verify_fn):
    obs = make_point_obs(n_time=4, n_points=2, fill=0.0)
    obs["precip"].values[:] = [[5.0, 0.0, 5.0, 0.0], [0.0, 5.0, 0.0, 5.0]]
    fc = obs.copy(deep=True)
    fc["precip"].values += 1.0
    ds = _verify(tmp_path, verify_fn, fc, obs, "--metric", "rmse")
    np.testing.assert_allclose(ds["rmse"].values, [1.0, 1.0])
    roc = _verify(tmp_path, verify_fn, fc, obs, "--metric", "roc", "--threshold", "3")
    assert roc.attrs["roc_area"] == pytest.approx(1.0)

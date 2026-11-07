"""Correctness tests for verify: statistics, then metrics from grouped statistics."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def verify_fn():
    return load_skill("verify", "verify").verify


@pytest.fixture(scope="module")
def summarize_fn():
    return load_skill("summarize-dim", "summarize_dim").summarize_dim


def _verify(tmp_path, verify_fn, fc, obs, *args, name="out.zarr"):
    pf = write_zarr(fc, tmp_path / f"fc_{name}")
    po = write_zarr(obs, tmp_path / f"obs_{name}")
    out = tmp_path / name
    run_skill(verify_fn, "--forecast", str(pf), "--obs", str(po), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True)


def _stats(tmp_path, verify_fn, fc, obs, *stats, extra=()):
    args = [a for s in stats for a in ("--statistic", s)]
    return _verify(tmp_path, verify_fn, fc, obs, *args, *extra)


def _group(tmp_path, summarize_fn, path, *args, name="grouped.zarr"):
    out = tmp_path / name
    run_skill(summarize_fn, "-i", str(path), "-o", str(out), *args)
    return out


def _all_mean(tmp_path, summarize_fn, path, *dims, name="grouped.zarr"):
    flags = [a for d in dims for a in ("--dim", d)]
    return _group(tmp_path, summarize_fn, path, *flags, "--method", "mean", name=name)


def _metric(tmp_path, verify_fn, path, metric, name="metric.zarr"):
    out = tmp_path / name
    run_skill(verify_fn, "--metric", metric, "-i", str(path), "-o", str(out))
    return xr.open_zarr(out, consolidated=True)


def _random_pair(seed, n_time=30, lats=(1.0, 2.0, 3.0), lons=(10.0, 11.0)):
    rng = np.random.default_rng(seed)
    obs = make_gridded(n_time=n_time, lats=lats, lons=lons)
    obs["precip"].values = rng.gamma(1.0, 4.0, obs["precip"].shape)
    fc = obs.copy(deep=True)
    fc["precip"].values = obs["precip"].values * 0.7 + rng.gamma(1.0, 2.0, obs["precip"].shape)
    return fc, obs


def _temp(value, units):
    ds = make_gridded(n_time=1, fill=value, name="t2m")
    ds["t2m"].attrs = {"units": units, "standard_name": "air_temperature"}
    return ds


def _expect_exit(fn, *args, **kwargs):
    with pytest.raises(SystemExit) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == 2


# --- statistics ---------------------------------------------------------------------------


def test_error_statistics_and_attrs(tmp_path, verify_fn, capsys):
    fc = make_gridded(n_time=1, fill=3.0)
    obs = make_gridded(n_time=1, fill=1.0)
    ds = _stats(tmp_path, verify_fn, fc, obs, "fcst", "obs", "bias", "mae", "mse", "covariance")
    assert float(ds["fcst"].max()) == 3.0 and float(ds["obs"].max()) == 1.0
    assert float(ds["bias"].max()) == 2.0 and float(ds["mae"].max()) == 2.0
    assert float(ds["mse"].max()) == 4.0 and float(ds["covariance"].max()) == 3.0
    assert ds["bias"].attrs["units"] == "mm day-1"
    assert ds["mse"].attrs["units"] != "mm day-1"
    assert set(ds["n_valid"].values.ravel()) == {1.0}
    runs = {ds[v].attrs["verify_run"] for v in ds.data_vars}
    assert len(runs) == 1
    assert ds["mse"].attrs["verify_statistic"] == "mse"
    assert json.loads(ds["mse"].attrs["verify_config"])["forecast_variable"] == "precip"
    assert "12 samples" in capsys.readouterr().out
    assert load_history(tmp_path / "out.zarr")[-1]["skill"] == "verify"


def test_contingency_statistics(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, fill=0.0)
    obs = make_gridded(n_time=1, fill=0.0)
    fc["precip"].values[0, 0, :2] = 5.0  # forecast events at (0,0), (0,1)
    obs["precip"].values[0, 0, 0] = 5.0  # observed events at (0,0), (1,0)
    obs["precip"].values[0, 1, 0] = 5.0
    ds = _stats(
        tmp_path, verify_fn, fc, obs,
        "true_positives", "false_positives", "false_negatives", "true_negatives",
        extra=("--threshold", "1"),
    )  # fmt: skip
    one = {s: ds[s].isel(time=0).values for s in ds.data_vars}
    assert one["true_positives"][0, 0] == 1 and one["true_positives"].sum() == 1
    assert one["false_positives"][0, 1] == 1 and one["false_positives"].sum() == 1
    assert one["false_negatives"][1, 0] == 1 and one["false_negatives"].sum() == 1
    assert one["true_negatives"].sum() == 12 - 3


def test_flags_must_match_statistics(tmp_path, verify_fn):
    fc, obs = make_gridded(n_time=1), make_gridded(n_time=1)
    run = (tmp_path, verify_fn, fc, obs)
    _expect_exit(_stats, *run, "true_positives")  # no --threshold
    _expect_exit(_stats, *run, "mae", extra=("--threshold", "1"))  # threshold unused
    _expect_exit(_stats, *run, "squared_fcst_anom")  # no --clim
    _expect_exit(_stats, *run, "n_correct")  # no --bins
    _expect_exit(_stats, *run, "crps_fair")  # no members
    _expect_exit(_stats, *run, "mae", extra=("--member-fraction", "0.5"))
    _expect_exit(_verify, *run)  # neither --statistic nor --metric


def test_single_init_scores_each_lead_against_its_valid_date(tmp_path, verify_fn):
    """Step s of a 2026-01-01 init pairs with obs on 2026-01-01 + s, no step-to-time first."""
    fc = make_forecast(n_step=2, fill=5.0, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[1] = 0.0  # lead 1 day forecasts no event
    obs = make_gridded(n_time=3, fill=5.0)
    obs["precip"].values[2] = 0.0  # 2026-01-03 has none; no lead lands there
    ds = _stats(
        tmp_path, verify_fn, fc, obs, "true_positives", "false_negatives",
        extra=("--threshold", "1"),
    )  # fmt: skip
    assert ds["true_positives"].dims[:2] == ("time", "step")
    assert (ds["true_positives"].isel(step=0).values == 1).all()
    assert (ds["false_negatives"].isel(step=1).values == 1).all()


def test_init_window_leads_past_obs_record_stay_nan(tmp_path, verify_fn):
    """Two inits x two leads: valid times without obs are NaN, never filled."""
    fc = make_forecast(n_step=2, name="precip", fill=3.0)
    fc["precip"].attrs["units"] = "mm day-1"
    fc = xr.concat([fc, fc.assign_coords(time=np.datetime64("2026-01-02", "ns"))], dim="time")
    obs = make_gridded(n_time=2, fill=1.0)  # 2026-01-01 and 2026-01-02 only
    bias = _stats(tmp_path, verify_fn, fc, obs, "bias")["bias"]
    np.testing.assert_allclose(bias.isel(time=0).values, 2.0)
    np.testing.assert_allclose(bias.isel(time=1, step=0).values, 2.0)
    assert np.isnan(bias.isel(time=1, step=1).values).all()  # 2026-01-03: no obs


def test_leads_with_no_obs_at_any_valid_time_are_refused(tmp_path, verify_fn, capsys):
    fc = make_forecast(n_step=2, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    obs = make_gridded(n_time=2, start="2027-01-01")
    _expect_exit(_stats, tmp_path, verify_fn, fc, obs, "bias")
    assert "no --obs time matches a forecast valid time" in capsys.readouterr().err


def test_obs_finer_grid_is_refused(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0))
    obs = make_gridded(n_time=1, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0))
    _expect_exit(_stats, tmp_path, verify_fn, fc, obs, "bias")


def test_units_converted_and_offset_units_scored_in_kelvin(tmp_path, verify_fn):
    """deg C forecast vs K obs: bias +2 K, not -271."""
    ds = _stats(
        tmp_path, verify_fn, _temp(27.0, "degree_Celsius"), _temp(298.15, "K"), "bias", "mse"
    )
    assert float(ds["bias"].max()) == pytest.approx(2.0)
    assert ds["bias"].attrs["units"] == "K"
    assert float(ds["mse"].max()) == pytest.approx(4.0)

    hourly = make_gridded(n_time=1, fill=0.5)
    hourly["precip"].attrs["units"] = "mm hr-1"
    ds = _stats(tmp_path, verify_fn, make_gridded(n_time=1, fill=24.0), hourly, "mae")
    assert float(ds["mae"].max()) == pytest.approx(12.0)
    assert ds["mae"].attrs["units"] == "mm day-1"


def test_threshold_applies_in_forecast_units(tmp_path, verify_fn):
    """36 deg C forecast vs 300 K (26.85 deg C) obs at 35 degC: a false alarm."""
    ds = _stats(
        tmp_path, verify_fn, _temp(36.0, "degree_Celsius"), _temp(300.0, "K"),
        "false_positives", extra=("--threshold", "35 degC"),
    )  # fmt: skip
    assert (ds["false_positives"].values == 1).all()


def test_unit_mismatches_are_refused(tmp_path, verify_fn):
    total = make_gridded(n_time=1, fill=14.0)
    total["precip"].attrs = {"units": "mm"}
    _expect_exit(_stats, tmp_path, verify_fn, total, make_gridded(n_time=1), "mae")
    fc = make_gridded(n_time=1, fill=3.0, name="x")
    fc["x"].attrs = {"units": "K"}
    obs = make_gridded(n_time=1, fill=1.0, name="x")
    obs["x"].attrs = {}
    _expect_exit(_stats, tmp_path, verify_fn, fc, obs, "bias")
    _expect_exit(_stats, tmp_path, verify_fn, obs, fc, "bias")


def test_total_threshold_on_a_rate_uses_its_aggregation_period(tmp_path, verify_fn, capsys):
    """50 mm over 3-day mean rates is 16.67 mm day-1: 17 is an event, 16 is not."""
    fc = make_gridded(n_time=1, fill=17.0)
    fc["precip"].attrs["aggregation_period"] = "3 day"
    fc["precip"].values[0, 0, 0] = 16.0
    obs = make_gridded(n_time=1, fill=17.0)
    ds = _stats(
        tmp_path, verify_fn, fc, obs, "true_positives", "false_negatives",
        extra=("--threshold", "50 mm"),
    )  # fmt: skip
    assert ds["false_negatives"].values[0, 0, 0] == 1
    assert ds["true_positives"].values.sum() == 11
    assert json.loads(ds["true_positives"].attrs["verify_config"])["threshold"] == pytest.approx(
        50 / 3
    )
    assert "50 mm over 3 day" in capsys.readouterr().err

    fc["precip"].attrs.pop("aggregation_period")
    fc["precip"].attrs.pop("data_interval", None)
    _expect_exit(
        _stats, tmp_path, verify_fn, fc, obs, "true_positives", extra=("--threshold", "50 mm")
    )


def test_member_fraction_counts_members_at_the_threshold(tmp_path, verify_fn):
    """2 of 4 members >= 10: an event at --member-fraction 0.5, not at 0.75 or by the mean."""
    fc = make_forecast(n_step=1, members=4, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[:] = np.array([0.0, 4.0, 10.0, 10.0])[:, None, None, None]
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=12.0)
    for fraction, hit in (("0.5", 1), ("0.75", 0)):
        ds = _stats(
            tmp_path, verify_fn, fc, obs, "true_positives",
            extra=("--threshold", "10", "--member-fraction", fraction),
        )  # fmt: skip
        assert (ds["true_positives"].values == hit).all()
    ds = _stats(tmp_path, verify_fn, fc, obs, "true_positives", extra=("--threshold", "10"))
    assert (ds["true_positives"].values == 0).all()


def test_ensemble_mean_and_nan_member(tmp_path, verify_fn):
    """Deterministic statistics score the ensemble mean; a NaN member drops the sample."""
    fc = make_forecast(n_step=1, members=3, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[:] = np.array([0.0, 0.0, 3.0])[:, None, None, None]
    fc["precip"].values[2, 0, 0, 0] = np.nan
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=0.0)
    ds = _stats(tmp_path, verify_fn, fc, obs, "mae")
    mae = ds["mae"].isel(time=0).values
    assert np.isnan(mae[0, 0])
    np.testing.assert_allclose(mae.ravel()[1:], 1.0)
    assert np.isnan(ds["n_valid"].isel(time=0).values[0, 0])


def test_station_pairs(tmp_path, verify_fn):
    ds = _stats(tmp_path, verify_fn, make_point_obs(fill=3.0), make_point_obs(fill=1.0), "bias")
    assert ds["bias"].attrs["units"] == "mm day-1"
    assert float(ds["bias"].mean()) == pytest.approx(2.0)


def test_scalar_times_must_match(tmp_path, verify_fn, capsys):
    fc = make_gridded(n_time=1, fill=3.0).isel(time=0)
    obs = make_gridded(n_time=1, fill=1.0, start="2026-02-01").isel(time=0)
    _expect_exit(_stats, tmp_path, verify_fn, fc, obs, "bias")
    assert "no overlapping time labels" in capsys.readouterr().err
    ds = _stats(tmp_path, verify_fn, fc, obs.assign_coords(time=fc.time), "bias")
    assert float(ds["bias"].max()) == pytest.approx(2.0)


def test_one_sample_set_for_every_statistic(tmp_path, verify_fn):
    """A sample missing from one statistic is missing from all, so their means pool alike."""
    fc = make_gridded(n_time=1, fill=2.0)
    obs = make_gridded(n_time=1, fill=1.0)
    obs["precip"].values[0, 0, 0] = np.nan
    ds = _stats(tmp_path, verify_fn, fc, obs, "fcst", "obs", "covariance")
    for v in ds.data_vars:
        assert np.isnan(ds[v].values[0, 0, 0])
        assert int(ds[v].notnull().sum()) == 11


def test_smape_of_two_zeros_is_zero(tmp_path, verify_fn):
    ds = _stats(
        tmp_path, verify_fn, make_gridded(n_time=1, fill=0.0), make_gridded(n_time=1, fill=0.0),
        "smape", "mape",
    )  # fmt: skip
    assert (ds["smape"].values == 0).all() and (ds["mape"].values == 0).all()


def test_filter_keeps_only_masked_samples(tmp_path, verify_fn):
    """Two masks AND together; a NaN mask value leaves the sample out."""
    fc, obs = make_gridded(n_time=2, fill=3.0), make_gridded(n_time=2, fill=1.0)
    a = make_gridded(n_time=2, fill=1.0, name="indicator")
    a["indicator"].attrs = {"units": "1"}
    a["indicator"].values[0] = 0.0  # first day out
    b = a.copy(deep=True)
    b["indicator"].values[:] = 1.0
    b["indicator"].values[1, 0, 0] = np.nan  # one cell on day two out
    pa, pb = write_zarr(a, tmp_path / "a.zarr"), write_zarr(b, tmp_path / "b.zarr")
    ds = _stats(
        tmp_path, verify_fn, fc, obs, "mae", extra=("--filter", str(pa), "--filter", str(pb))
    )
    assert int(ds["mae"].notnull().sum()) == 11
    assert ds["mae"].isel(time=0).isnull().all()
    assert json.loads(ds["mae"].attrs["verify_config"])["filters"] == 2


def test_obs_filter_follows_forecast_leads(tmp_path, verify_fn):
    """A time-only obs mask applies at each init + step, like the obs."""
    fc = make_forecast(n_step=2, name="precip", fill=3.0)
    fc["precip"].attrs["units"] = "mm day-1"
    obs = make_gridded(n_time=2, fill=1.0)
    mask = make_gridded(n_time=2, fill=1.0, name="indicator")
    mask["indicator"].attrs = {"units": "1"}
    mask["indicator"].values[1] = 0.0  # 2026-01-02 is not an event day
    pm = write_zarr(mask, tmp_path / "m.zarr")
    mae = _stats(tmp_path, verify_fn, fc, obs, "mae", extra=("--filter", str(pm)))["mae"]
    assert mae.isel(step=0).notnull().all() and mae.isel(step=1).isnull().all()


def test_anomaly_statistics_use_clim(tmp_path, verify_fn):
    fc, obs = make_gridded(n_time=1, fill=5.0), make_gridded(n_time=1, fill=3.0)
    clim = make_gridded(n_time=1, fill=4.0, name="precip_avg")
    pc = write_zarr(clim, tmp_path / "clim.zarr")
    ds = _stats(
        tmp_path, verify_fn, fc, obs, "squared_fcst_anom", "squared_obs_anom", "anom_covariance",
        extra=("--clim", str(pc)),
    )  # fmt: skip
    assert float(ds["squared_fcst_anom"].max()) == 1.0
    assert float(ds["anom_covariance"].max()) == -1.0


def test_brier_from_members_and_from_a_probability(tmp_path, verify_fn):
    fc = make_forecast(n_step=1, members=4, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[:] = np.array([0.0, 4.0, 10.0, 12.0])[:, None, None, None]
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=12.0)
    ds = _stats(tmp_path, verify_fn, fc, obs, "brier", "obs_event", extra=("--threshold", "10"))
    np.testing.assert_allclose(ds["brier"].values, 0.25)  # p = 0.5, o = 1
    assert (ds["obs_event"].values == 1).all()

    prob = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=0.8, name="p")
    prob["p"].attrs = {"units": "1"}
    mask = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=1.0, name="p")
    mask["p"].attrs = {"units": "1"}
    mask["p"].values[0, 0, 0] = 0.0
    ds = _stats(
        tmp_path, verify_fn, prob, mask, "brier", extra=("--threshold", "1", "--probability")
    )
    assert ds["brier"].values[0, 0, 0] == pytest.approx(0.64)
    assert ds["brier"].values[0, 1, 1] == pytest.approx(0.04)

    prob["p"].values[0, 0, 0] = 1.5
    _expect_exit(
        _stats, tmp_path, verify_fn, fc, obs, "brier", extra=("--threshold", "1", "--probability")
    )


def test_crps_matches_the_pairwise_definition(tmp_path, verify_fn):
    rng = np.random.default_rng(0)
    fc = make_forecast(n_step=1, members=5, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values = rng.gamma(1.0, 3.0, fc["precip"].shape)
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=2.0)
    ds = _stats(tmp_path, verify_fn, fc, obs, "crps", "crps_fair", "ensemble_variance")
    x = fc["precip"].isel(time=0, latitude=0, longitude=0).values
    m = x.size
    pair = np.abs(x[:, None] - x[None, :]).sum()
    assert float(ds["crps"].values[0, 0, 0]) == pytest.approx(
        np.abs(x - 2).mean() - pair / (2 * m * m), rel=1e-5
    )
    assert float(ds["crps_fair"].values[0, 0, 0]) == pytest.approx(
        np.abs(x - 2).mean() - pair / (2 * m * (m - 1)), rel=1e-5
    )
    assert float(ds["ensemble_variance"].values[0, 0, 0]) == pytest.approx(
        x.var(ddof=1) * (m + 1) / m, rel=1e-5
    )


def test_extra_dims_pass_through(tmp_path, verify_fn):
    """Several models stacked on a new dim are scored against one obs at once."""
    fc = xr.concat(
        [make_gridded(n_time=1, fill=v) for v in (2.0, 4.0)],
        dim=xr.DataArray(["a", "b"], dims="model"),
    )
    ds = _stats(tmp_path, verify_fn, fc, make_gridded(n_time=1, fill=1.0), "bias")
    assert float(ds["bias"].sel(model="a").max()) == 1.0
    assert float(ds["bias"].sel(model="b").max()) == 3.0


# --- metrics from grouped statistics ------------------------------------------------------


def _pipeline(tmp_path, verify_fn, summarize_fn, fc, obs, metric, *stats, extra=(), dims=None):
    _stats(tmp_path, verify_fn, fc, obs, *stats, extra=extra)
    dims = dims or ("time", "latitude", "longitude")
    grouped = _all_mean(tmp_path, summarize_fn, tmp_path / "out.zarr", *dims)
    return _metric(tmp_path, verify_fn, grouped, metric)


def test_rmse_is_the_root_of_the_pooled_mean(tmp_path, verify_fn, summarize_fn):
    fc, obs = _random_pair(1)
    ds = _pipeline(tmp_path, verify_fn, summarize_fn, fc, obs, "rmse", "mse")
    err = fc["precip"].values - obs["precip"].values
    assert float(ds["rmse"]) == pytest.approx(np.sqrt((err**2).mean()), rel=1e-5)
    assert ds["rmse"].attrs["units"] == "mm day-1"
    assert ds.attrs["verify_metric"] == "rmse"


def test_rmse_per_cell_over_time(tmp_path, verify_fn, summarize_fn):
    fc, obs = _random_pair(2)
    ds = _pipeline(tmp_path, verify_fn, summarize_fn, fc, obs, "rmse", "mse", dims=("time",))
    err = fc["precip"].values - obs["precip"].values
    np.testing.assert_allclose(ds["rmse"].values, np.sqrt((err**2).mean(0)), rtol=1e-5)


def test_pearson_matches_numpy(tmp_path, verify_fn, summarize_fn):
    fc, obs = _random_pair(3)
    stats = ("fcst", "obs", "squared_fcst", "squared_obs", "covariance")
    ds = _pipeline(tmp_path, verify_fn, summarize_fn, fc, obs, "pearson", *stats)
    r = np.corrcoef(fc["precip"].values.ravel(), obs["precip"].values.ravel())[0, 1]
    assert float(ds["pearson"]) == pytest.approx(r, rel=1e-4)


def test_contingency_metrics(tmp_path, verify_fn, summarize_fn):
    fc, obs = _random_pair(4)
    stats = ("true_positives", "false_positives", "false_negatives", "true_negatives")
    _stats(tmp_path, verify_fn, fc, obs, *stats, extra=("--threshold", "4"))
    grouped = _all_mean(
        tmp_path, summarize_fn, tmp_path / "out.zarr", "time", "latitude", "longitude"
    )
    f, o = fc["precip"].values >= 4, obs["precip"].values >= 4
    a, b, c, d = ((x & y).sum() for x, y in ((f, o), (f, ~o), (~f, o), (~f, ~o)))
    expected = {
        "pod": a / (a + c),
        "far": b / (a + b),
        "pofd": b / (b + d),
        "csi": a / (a + b + c),
        "frequency_bias": (a + b) / (a + c),
        "ets": (a - (a + b) * (a + c) / (a + b + c + d))
        / (a + b + c - (a + b) * (a + c) / (a + b + c + d)),
    }
    for metric, value in expected.items():
        ds = _metric(tmp_path, verify_fn, grouped, metric, name=f"{metric}.zarr")
        assert float(ds[metric]) == pytest.approx(value, rel=1e-5), metric
        assert ds[metric].attrs["units"] == "1"


def test_brier_skill_score_vs_base_rate(tmp_path, verify_fn, summarize_fn):
    fc, obs = _random_pair(5)
    ds = _pipeline(
        tmp_path, verify_fn, summarize_fn, fc, obs, "bss", "brier", "obs_event",
        extra=("--threshold", "4"),
    )  # fmt: skip
    p = (fc["precip"].values >= 4).astype(float)
    o = (obs["precip"].values >= 4).astype(float)
    expected = 1 - ((p - o) ** 2).mean() / (o.mean() * (1 - o.mean()))
    assert float(ds["bss"]) == pytest.approx(expected, rel=1e-5)


def test_heidke_with_bins(tmp_path, verify_fn, summarize_fn):
    fc, obs = _random_pair(6)
    ds = _pipeline(
        tmp_path, verify_fn, summarize_fn, fc, obs, "heidke", "n_correct", "n_fcst_bin",
        "n_obs_bin", extra=("--bins", "2", "6"),
    )  # fmt: skip
    f = np.digitize(fc["precip"].values.ravel(), [2, 6])
    o = np.digitize(obs["precip"].values.ravel(), [2, 6])
    chance = sum((f == k).mean() * (o == k).mean() for k in range(3))
    expected = ((f == o).mean() - chance) / (1 - chance)
    assert float(ds["heidke"]) == pytest.approx(expected, rel=1e-5)


def test_rpss_from_edges(tmp_path, verify_fn, summarize_fn):
    rng = np.random.default_rng(7)
    fc = xr.concat(
        [make_gridded(n_time=20, lats=(1.0,), lons=(10.0,)) for _ in range(6)], dim="number"
    )
    fc["precip"].values = rng.gamma(1.0, 3.0, fc["precip"].shape)
    obs = make_gridded(n_time=20, lats=(1.0,), lons=(10.0,))
    obs["precip"].values = rng.gamma(1.0, 3.0, obs["precip"].shape)
    edges = make_gridded(n_time=2, lats=(1.0,), lons=(10.0,)).isel(time=0, drop=True)
    edges = edges.expand_dims(quantile=[0.333, 0.667]).copy(deep=True)
    edges["precip"].values[:] = np.array([1.5, 3.5])[:, None, None]
    pe = write_zarr(edges, tmp_path / "edges.zarr")
    ds = _pipeline(
        tmp_path, verify_fn, summarize_fn, fc, obs, "rpss", "rps", "rps_clim",
        extra=("--fc-edges", str(pe), "--obs-edges", str(pe)),
    )  # fmt: skip
    x, y = fc["precip"].values[:, :, 0, 0], obs["precip"].values[:, 0, 0]
    cdf = np.stack([(x <= e).mean(0) for e in (1.5, 3.5)], -1)
    ocdf = np.stack([(y <= e) for e in (1.5, 3.5)], -1).astype(float)
    rps = ((cdf - ocdf) ** 2).sum(-1)
    clim = ((np.array([1 / 3, 2 / 3]) - ocdf) ** 2).sum(-1)
    assert float(ds["rpss"]) == pytest.approx(1 - rps.mean() / clim.mean(), rel=1e-5)


def test_reliability_and_roc_curves(tmp_path, verify_fn, summarize_fn):
    rng = np.random.default_rng(8)
    prob = make_gridded(n_time=40, name="p")
    prob["p"].values = rng.uniform(0, 1, prob["p"].shape)
    prob["p"].attrs = {"units": "1"}
    event = prob.copy(deep=True)
    event["p"].values = (rng.uniform(0, 1, prob["p"].shape) < prob["p"].values).astype(float)
    stats = ("reliability_count", "reliability_fcst", "reliability_obs")
    ds = _pipeline(
        tmp_path, verify_fn, summarize_fn, prob, event, "reliability", *stats,
        extra=("--threshold", "1", "--probability"),
    )  # fmt: skip
    p, o = prob["p"].values.ravel(), event["p"].values.ravel()
    k = np.clip(np.floor(p * 10 + 1e-9), 0, 9)
    assert float(ds["sample_fraction"].sum()) == pytest.approx(1.0, rel=1e-5)
    assert float(ds["observed_frequency"][3]) == pytest.approx(o[k == 3].mean(), rel=1e-5)
    assert float(ds["forecast_probability"][3]) == pytest.approx(p[k == 3].mean(), rel=1e-5)

    ds = _pipeline(
        tmp_path, verify_fn, summarize_fn, prob, event, "roc", "roc_hits", "roc_false_alarms",
        "obs_event", extra=("--threshold", "1", "--probability"),
    )  # fmt: skip
    hit = [((p >= t) & (o == 1)).sum() / (o == 1).sum() for t in np.arange(0.1, 1.01, 0.1)]
    np.testing.assert_allclose(ds["hit_rate"].values, hit, rtol=1e-5)
    assert 0.6 < float(ds["roc_area"]) <= 1.0


def test_spread_skill(tmp_path, verify_fn, summarize_fn):
    rng = np.random.default_rng(9)
    fc = xr.concat([make_gridded(n_time=30) for _ in range(5)], dim="number")
    fc["precip"].values = rng.normal(5, 1, fc["precip"].shape)
    obs = make_gridded(n_time=30)
    obs["precip"].values = rng.normal(5, 1, obs["precip"].shape)
    ds = _pipeline(tmp_path, verify_fn, summarize_fn, fc, obs, "spread", "ensemble_variance", "mse")
    x, y = fc["precip"].values, obs["precip"].values
    var = (x.var(0, ddof=1) * 6 / 5).mean()
    assert float(ds["spread"]) == pytest.approx(
        np.sqrt(var / ((x.mean(0) - y) ** 2).mean()), rel=1e-4
    )


def test_metric_per_group_keeps_the_group_dim(tmp_path, verify_fn, summarize_fn):
    fc, obs = _random_pair(10)
    obs = obs.assign_coords(zone=("latitude", ["n", "n", "s"]))
    fc = fc.assign_coords(zone=("latitude", ["n", "n", "s"]))
    _stats(tmp_path, verify_fn, fc, obs, "mse")
    grouped = _group(
        tmp_path, summarize_fn, tmp_path / "out.zarr", "--dim", "time", "--dim", "longitude", "--by", "zone",
        "--method", "mean",
    )  # fmt: skip
    ds = _metric(tmp_path, verify_fn, grouped, "rmse")
    err = fc["precip"].values - obs["precip"].values
    assert float(ds["rmse"].sel(zone="s")) == pytest.approx(
        np.sqrt((err[:, 2] ** 2).mean()), rel=1e-5
    )


# --- metric refusals ----------------------------------------------------------------------


def test_metric_missing_statistic(tmp_path, verify_fn, summarize_fn, capsys):
    fc, obs = _random_pair(11)
    _stats(tmp_path, verify_fn, fc, obs, "fcst", "obs")
    grouped = _all_mean(tmp_path, summarize_fn, tmp_path / "out.zarr", "time")
    _expect_exit(_metric, tmp_path, verify_fn, grouped, "pearson")
    assert "squared_fcst" in capsys.readouterr().err


def test_metric_refuses_statistics_from_different_calls(tmp_path, verify_fn, summarize_fn, capsys):
    fc, obs = _random_pair(12)
    a = _stats(tmp_path, verify_fn, fc, obs, "fcst", "obs", "squared_fcst", "squared_obs")
    b = _stats(tmp_path, verify_fn, fc, obs, "covariance")
    write_zarr(xr.merge([a, b[["covariance"]]], combine_attrs="override"), tmp_path / "mix.zarr")
    grouped = _all_mean(tmp_path, summarize_fn, tmp_path / "mix.zarr", "time")
    _expect_exit(_metric, tmp_path, verify_fn, grouped, "pearson")
    assert "different verify calls" in capsys.readouterr().err


def test_metric_refuses_ungrouped_and_non_mean(tmp_path, verify_fn, summarize_fn, capsys):
    fc, obs = _random_pair(13)
    _stats(tmp_path, verify_fn, fc, obs, "mse")
    _expect_exit(_metric, tmp_path, verify_fn, tmp_path / "out.zarr", "rmse")
    assert "never grouped" in capsys.readouterr().err
    summed = _group(
        tmp_path, summarize_fn, tmp_path / "out.zarr", "--dim", "time", "--method", "median"
    )
    _expect_exit(_metric, tmp_path, verify_fn, summed, "rmse")
    assert "median" in capsys.readouterr().err


def test_metric_refuses_statistics_reduced_differently(tmp_path, verify_fn, summarize_fn, capsys):
    fc, obs = _random_pair(14)
    stats = ("true_positives", "false_negatives")
    _stats(tmp_path, verify_fn, fc, obs, *stats, extra=("--threshold", "4"))
    a = _group(tmp_path, summarize_fn, tmp_path / "out.zarr", "--dim", "time", "--method",
               "mean", "-v", "true_positives", name="a.zarr")  # fmt: skip
    ds = xr.open_zarr(a)
    ds["false_negatives"] = ds["false_negatives"].mean("latitude", keep_attrs=True)
    ds["false_negatives"].attrs["cell_methods"] = "latitude: mean"
    write_zarr(ds.compute(), tmp_path / "b.zarr")
    _expect_exit(_metric, tmp_path, verify_fn, tmp_path / "b.zarr", "pod")
    assert "reduced differently" in capsys.readouterr().err


def test_metric_takes_no_forecast_flags(tmp_path, verify_fn):
    fc, obs = _random_pair(15)
    _stats(tmp_path, verify_fn, fc, obs, "mse")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            verify_fn, "--metric", "rmse", "-i", str(tmp_path / "out.zarr"),
            "--threshold", "1", "-o", str(tmp_path / "m.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2

"""Correctness tests for verify: per-sample statistics, pooled over groups, into metrics."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def verify_fn():
    return load_skill("verify", "verify").verify


def _verify(tmp_path, verify_fn, fc, obs, *args, name="out.zarr"):
    pf = write_zarr(fc, tmp_path / f"fc_{name}")
    po = write_zarr(obs, tmp_path / f"obs_{name}")
    out = tmp_path / name
    run_skill(verify_fn, "--forecast", str(pf), "--obs", str(po), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True)


def _score(tmp_path, verify_fn, fc, obs, *metrics, extra=(), keep=None):
    """The metrics, keeping the dims in ``keep`` (None: every forecast dim, one value per
    sample; () pools everything)."""
    if keep is None:
        keep = [d for d in fc.dims if d not in ("number", "quintile")]
    args = [a for m in metrics for a in ("--metric", m)] + [a for d in keep for a in ("--keep", d)]
    return _verify(tmp_path, verify_fn, fc, obs, *args, *extra)


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


# --- per-sample values (no grouping) ------------------------------------------------------


def test_error_metrics_per_sample_and_attrs(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, fill=3.0)
    obs = make_gridded(n_time=1, fill=1.0)
    ds = _score(tmp_path, verify_fn, fc, obs, "fcst", "obs", "bias", "mae", "mse")
    assert float(ds["fcst"].max()) == 3.0 and float(ds["obs"].max()) == 1.0
    assert float(ds["bias"].max()) == 2.0 and float(ds["mae"].max()) == 2.0
    assert float(ds["mse"].max()) == 4.0
    assert ds["bias"].attrs["units"] == "mm day-1"
    assert ds["mse"].attrs["units"] != "mm day-1"
    assert set(ds["n_valid"].values.ravel()) == {1.0}
    assert ds["mse"].attrs["verify_metric"] == "mse"
    assert json.loads(ds["mse"].attrs["verify_config"])["forecast_variable"] == "precip"
    assert ds.attrs["verify_metrics"] == "fcst obs bias mae mse"
    assert load_history(tmp_path / "out.zarr")[-1]["skill"] == "verify"


def test_flags_must_match_metrics(tmp_path, verify_fn):
    fc, obs = make_gridded(n_time=1), make_gridded(n_time=1)
    run = (tmp_path, verify_fn, fc, obs)
    _expect_exit(_score, *run, "pod")  # no --threshold
    _expect_exit(_score, *run, "mae", extra=("--threshold", "1"))  # threshold unused
    _expect_exit(_score, *run, "acc")  # no --clim
    _expect_exit(_score, *run, "heidke")  # no --bins
    _expect_exit(_score, *run, "crps_fair")  # no members
    _expect_exit(_score, *run, "mae", extra=("--member-fraction", "0.5"))
    _expect_exit(_verify, *run)  # no --metric


def test_single_init_scores_each_lead_against_its_valid_date(tmp_path, verify_fn):
    """Step s of a 2026-01-01 init pairs with obs on 2026-01-01 + s, no step-to-time first."""
    fc = make_forecast(n_step=2, fill=5.0, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[1] = 0.0  # lead 1 day forecasts no event
    obs = make_gridded(n_time=3, fill=5.0)
    obs["precip"].values[2] = 0.0  # 2026-01-03 has none; no lead lands there
    ds = _score(tmp_path, verify_fn, fc, obs, "pod", extra=("--threshold", "1"), keep=("step",))
    assert ds["pod"].dims == ("step",)
    np.testing.assert_allclose(ds["pod"].values, [1.0, 0.0])


def test_init_window_leads_past_obs_record_stay_nan(tmp_path, verify_fn):
    """Two inits x two leads: valid times without obs are NaN, never filled."""
    fc = make_forecast(n_step=2, name="precip", fill=3.0)
    fc["precip"].attrs["units"] = "mm day-1"
    fc = xr.concat([fc, fc.assign_coords(time=np.datetime64("2026-01-02", "ns"))], dim="time")
    obs = make_gridded(n_time=2, fill=1.0)  # 2026-01-01 and 2026-01-02 only
    bias = _score(tmp_path, verify_fn, fc, obs, "bias")["bias"]
    np.testing.assert_allclose(bias.isel(time=0).values, 2.0)
    np.testing.assert_allclose(bias.isel(time=1, step=0).values, 2.0)
    assert np.isnan(bias.isel(time=1, step=1).values).all()  # 2026-01-03: no obs


def test_leads_with_no_obs_at_any_valid_time_are_refused(tmp_path, verify_fn, capsys):
    fc = make_forecast(n_step=2, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    obs = make_gridded(n_time=2, start="2027-01-01")
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "bias")
    assert "no --obs time matches a forecast valid time" in capsys.readouterr().err


def test_obs_finer_grid_is_refused(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0))
    obs = make_gridded(n_time=1, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0))
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "bias")


def test_units_converted_and_offset_units_scored_in_kelvin(tmp_path, verify_fn):
    """deg C forecast vs K obs: bias +2 K, not -271."""
    ds = _score(
        tmp_path, verify_fn, _temp(27.0, "degree_Celsius"), _temp(298.15, "K"), "bias", "mse"
    )
    assert float(ds["bias"].max()) == pytest.approx(2.0)
    assert ds["bias"].attrs["units"] == "K"
    assert float(ds["mse"].max()) == pytest.approx(4.0)

    hourly = make_gridded(n_time=1, fill=0.5)
    hourly["precip"].attrs["units"] = "mm hr-1"
    ds = _score(tmp_path, verify_fn, make_gridded(n_time=1, fill=24.0), hourly, "mae")
    assert float(ds["mae"].max()) == pytest.approx(12.0)
    assert ds["mae"].attrs["units"] == "mm day-1"


def test_threshold_applies_in_forecast_units(tmp_path, verify_fn):
    """36 deg C forecast vs 300 K (26.85 deg C) obs at 35 (deg C, scored in K): a false alarm."""
    ds = _score(
        tmp_path, verify_fn, _temp(36.0, "degree_Celsius"), _temp(300.0, "K"), "far",
        extra=("--threshold", "35"),
    )  # fmt: skip
    assert (ds["far"].values == 1).all()


def test_unit_mismatches_are_refused(tmp_path, verify_fn):
    total = make_gridded(n_time=1, fill=14.0)
    total["precip"].attrs = {"units": "mm"}
    _expect_exit(_score, tmp_path, verify_fn, total, make_gridded(n_time=1), "mae")
    fc = make_gridded(n_time=1, fill=3.0, name="x")
    fc["x"].attrs = {"units": "K"}
    obs = make_gridded(n_time=1, fill=1.0, name="x")
    obs["x"].attrs = {}
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "bias")
    _expect_exit(_score, tmp_path, verify_fn, obs, fc, "bias")


def test_zero_threshold_is_a_threshold(tmp_path, verify_fn):
    """--threshold 0 is a real cutoff, not a missing one: every value >= 0 is an event."""
    fc, obs = make_gridded(n_time=1, fill=0.0), make_gridded(n_time=1, fill=0.0)
    ds = _score(tmp_path, verify_fn, fc, obs, "pod", extra=("--threshold", "0"))
    assert (ds["pod"].values == 1).all()
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "pod", extra=("--threshold", "50 mm"))


def test_member_fraction_counts_members_at_the_threshold(tmp_path, verify_fn):
    """2 of 4 members >= 10: an event at --member-fraction 0.5, not at 0.75 or by the mean."""
    fc = make_forecast(n_step=1, members=4, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[:] = np.array([0.0, 4.0, 10.0, 10.0])[:, None, None, None]
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=12.0)
    for fraction, hit in (("0.5", 1), ("0.75", 0)):
        ds = _score(
            tmp_path, verify_fn, fc, obs, "pod",
            extra=("--threshold", "10", "--member-fraction", fraction),
        )  # fmt: skip
        assert (ds["pod"].values == hit).all()
    ds = _score(tmp_path, verify_fn, fc, obs, "pod", extra=("--threshold", "10"))
    assert (ds["pod"].values == 0).all()


def test_ensemble_mean_and_nan_member(tmp_path, verify_fn):
    """Deterministic metrics score the ensemble mean; a NaN member drops the sample."""
    fc = make_forecast(n_step=1, members=3, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[:] = np.array([0.0, 0.0, 3.0])[:, None, None, None]
    fc["precip"].values[2, 0, 0, 0] = np.nan
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=0.0)
    ds = _score(tmp_path, verify_fn, fc, obs, "mae")
    mae = ds["mae"].isel(time=0).values
    assert np.isnan(mae[0, 0])
    np.testing.assert_allclose(mae.ravel()[1:], 1.0)
    assert np.isnan(ds["n_valid"].isel(time=0).values[0, 0])


def test_station_pairs(tmp_path, verify_fn):
    ds = _score(tmp_path, verify_fn, make_point_obs(fill=3.0), make_point_obs(fill=1.0), "bias")
    assert ds["bias"].attrs["units"] == "mm day-1"
    assert float(ds["bias"].mean()) == pytest.approx(2.0)


def test_scalar_times_must_match(tmp_path, verify_fn, capsys):
    fc = make_gridded(n_time=1, fill=3.0).isel(time=0)
    obs = make_gridded(n_time=1, fill=1.0, start="2026-02-01").isel(time=0)
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "bias")
    assert "no overlapping time labels" in capsys.readouterr().err
    ds = _score(tmp_path, verify_fn, fc, obs.assign_coords(time=fc.time), "bias")
    assert float(ds["bias"].max()) == pytest.approx(2.0)


def test_one_sample_set_for_every_metric(tmp_path, verify_fn):
    """A sample missing from one input is missing from every metric, so they pool alike."""
    fc = make_gridded(n_time=1, fill=2.0)
    obs = make_gridded(n_time=1, fill=1.0)
    obs["precip"].values[0, 0, 0] = np.nan
    ds = _score(tmp_path, verify_fn, fc, obs, "fcst", "obs", "mse")
    for v in ("fcst", "obs", "mse"):
        assert np.isnan(ds[v].values[0, 0, 0])
        assert int(ds[v].notnull().sum()) == 11


def test_smape_of_two_zeros_is_zero(tmp_path, verify_fn):
    ds = _score(
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
    ds = _score(
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
    mae = _score(tmp_path, verify_fn, fc, obs, "mae", extra=("--filter", str(pm)))["mae"]
    assert mae.isel(step=0).notnull().all() and mae.isel(step=1).isnull().all()


def test_acc_uses_clim(tmp_path, verify_fn):
    """Forecast anomaly +1, obs anomaly -1 from a climatology of 4: ACC -1."""
    fc, obs = make_gridded(n_time=1, fill=5.0), make_gridded(n_time=1, fill=3.0)
    clim = make_gridded(n_time=1, fill=4.0, name="precip_avg")
    pc = write_zarr(clim, tmp_path / "clim.zarr")
    ds = _score(tmp_path, verify_fn, fc, obs, "acc", extra=("--clim", str(pc)))
    np.testing.assert_allclose(ds["acc"].values, -1.0)


def test_brier_from_members_and_from_a_probability(tmp_path, verify_fn):
    fc = make_forecast(n_step=1, members=4, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[:] = np.array([0.0, 4.0, 10.0, 12.0])[:, None, None, None]
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=12.0)
    ds = _score(tmp_path, verify_fn, fc, obs, "brier", extra=("--threshold", "10"))
    np.testing.assert_allclose(ds["brier"].values, 0.25)  # p = 0.5, o = 1

    prob = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=0.8, name="p")
    prob["p"].attrs = {"units": "1"}
    mask = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=1.0, name="p")
    mask["p"].attrs = {"units": "1"}
    mask["p"].values[0, 0, 0] = 0.0
    ds = _score(
        tmp_path, verify_fn, prob, mask, "brier", extra=("--threshold", "1", "--probability")
    )
    assert ds["brier"].values[0, 0, 0] == pytest.approx(0.64)
    assert ds["brier"].values[0, 1, 1] == pytest.approx(0.04)

    _expect_exit(  # members and --probability
        _score, tmp_path, verify_fn, fc, obs, "brier", extra=("--threshold", "1", "--probability")
    )
    prob["p"].values[0, 0, 0] = 1.5
    pf, pm = write_zarr(prob, tmp_path / "p.zarr"), write_zarr(mask, tmp_path / "m.zarr")
    with pytest.raises(SystemExit) as exc:  # not a probability
        run_skill(verify_fn, "--forecast", str(pf), "--obs", str(pm), "--metric", "brier",
                  "--threshold", "1", "--probability", "-o", str(tmp_path / "x.zarr"))  # fmt: skip
    assert exc.value.code != 0


def test_crps_and_spread_match_their_definitions(tmp_path, verify_fn):
    rng = np.random.default_rng(0)
    fc = make_forecast(n_step=1, members=5, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values = rng.gamma(1.0, 3.0, fc["precip"].shape)
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=2.0)
    ds = _score(tmp_path, verify_fn, fc, obs, "crps", "crps_fair", "spread")
    x = fc["precip"].isel(time=0, latitude=0, longitude=0).values
    m = x.size
    pair = np.abs(x[:, None] - x[None, :]).sum()
    assert float(ds["crps"].values[0, 0, 0]) == pytest.approx(
        np.abs(x - 2).mean() - pair / (2 * m * m), rel=1e-5
    )
    assert float(ds["crps_fair"].values[0, 0, 0]) == pytest.approx(
        np.abs(x - 2).mean() - pair / (2 * m * (m - 1)), rel=1e-5
    )
    assert float(ds["spread"].values[0, 0, 0]) == pytest.approx(
        np.sqrt(x.var(ddof=1) * (m + 1) / m / (x.mean() - 2) ** 2), rel=1e-5
    )


def test_extra_dims_pass_through(tmp_path, verify_fn):
    """Several models stacked on a new dim are scored against one obs at once."""
    fc = xr.concat(
        [make_gridded(n_time=1, fill=v) for v in (2.0, 4.0)],
        dim=xr.DataArray(["a", "b"], dims="model"),
    )
    ds = _score(tmp_path, verify_fn, fc, make_gridded(n_time=1, fill=1.0), "bias")
    assert float(ds["bias"].sel(model="a").max()) == 1.0
    assert float(ds["bias"].sel(model="b").max()) == 3.0


# --- pooled metrics -----------------------------------------------------------------------


def test_rmse_is_the_root_of_the_pooled_mean(tmp_path, verify_fn):
    fc, obs = _random_pair(1)
    ds = _score(tmp_path, verify_fn, fc, obs, "rmse", keep=())
    err = fc["precip"].values - obs["precip"].values
    assert float(ds["rmse"]) == pytest.approx(np.sqrt((err**2).mean()), rel=1e-5)
    assert ds["rmse"].attrs["units"] == "mm day-1"
    assert ds["rmse"].attrs["cell_methods"] == "time: latitude: longitude: mean"
    assert int(ds["n_valid"]) == err.size


def test_rmse_per_cell_over_time(tmp_path, verify_fn):
    fc, obs = _random_pair(2)
    ds = _score(tmp_path, verify_fn, fc, obs, "rmse", keep=("latitude", "longitude"))
    err = fc["precip"].values - obs["precip"].values
    np.testing.assert_allclose(ds["rmse"].values, np.sqrt((err**2).mean(0)), rtol=1e-5)


def test_pearson_matches_numpy(tmp_path, verify_fn):
    fc, obs = _random_pair(3)
    ds = _score(tmp_path, verify_fn, fc, obs, "pearson", keep=())
    r = np.corrcoef(fc["precip"].values.ravel(), obs["precip"].values.ravel())[0, 1]
    assert float(ds["pearson"]) == pytest.approx(r, rel=1e-4)


def test_contingency_metrics(tmp_path, verify_fn):
    fc, obs = _random_pair(4)
    expected_metrics = ("pod", "far", "pofd", "csi", "balanced_error_rate", "frequency_bias", "ets")
    ds = _score(
        tmp_path, verify_fn, fc, obs, *expected_metrics, extra=("--threshold", "4"), keep=()
    )
    f, o = fc["precip"].values >= 4, obs["precip"].values >= 4
    a, b, c, d = ((x & y).sum() for x, y in ((f, o), (f, ~o), (~f, o), (~f, ~o)))
    expected = {
        "pod": a / (a + c),
        "far": b / (a + b),
        "pofd": b / (b + d),
        "csi": a / (a + b + c),
        "balanced_error_rate": (c / (a + c) + b / (b + d)) / 2,
        "frequency_bias": (a + b) / (a + c),
        "ets": (a - (a + b) * (a + c) / (a + b + c + d))
        / (a + b + c - (a + b) * (a + c) / (a + b + c + d)),
    }
    for metric, value in expected.items():
        assert float(ds[metric]) == pytest.approx(value, rel=1e-5), metric
        assert ds[metric].attrs["units"] == "1"


def test_brier_skill_score_vs_base_rate(tmp_path, verify_fn):
    fc, obs = _random_pair(5)
    ds = _score(tmp_path, verify_fn, fc, obs, "bss", extra=("--threshold", "4"), keep=())
    p = (fc["precip"].values >= 4).astype(float)
    o = (obs["precip"].values >= 4).astype(float)
    expected = 1 - ((p - o) ** 2).mean() / (o.mean() * (1 - o.mean()))
    assert float(ds["bss"]) == pytest.approx(expected, rel=1e-5)


def test_heidke_with_bins(tmp_path, verify_fn):
    fc, obs = _random_pair(6)
    ds = _score(tmp_path, verify_fn, fc, obs, "heidke", extra=("--bins", "2", "6"), keep=())
    f = np.digitize(fc["precip"].values.ravel(), [2, 6])
    o = np.digitize(obs["precip"].values.ravel(), [2, 6])
    chance = sum((f == k).mean() * (o == k).mean() for k in range(3))
    expected = ((f == o).mean() - chance) / (1 - chance)
    assert float(ds["heidke"]) == pytest.approx(expected, rel=1e-5)


def _edges(tmp_path, values, lats=(1.0,), lons=(10.0,)):
    edges = make_gridded(n_time=1, lats=lats, lons=lons).isel(time=0, drop=True)
    edges = edges.expand_dims(quantile=[0.333, 0.667]).copy(deep=True)
    edges["precip"].values[:] = np.array(values)[:, None, None]
    return write_zarr(edges, tmp_path / "edges.zarr")


def test_rpss_from_edges(tmp_path, verify_fn):
    rng = np.random.default_rng(7)
    fc = xr.concat(
        [make_gridded(n_time=20, lats=(1.0,), lons=(10.0,)) for _ in range(6)], dim="number"
    )
    fc["precip"].values = rng.gamma(1.0, 3.0, fc["precip"].shape)
    obs = make_gridded(n_time=20, lats=(1.0,), lons=(10.0,))
    obs["precip"].values = rng.gamma(1.0, 3.0, obs["precip"].shape)
    pe = _edges(tmp_path, [1.5, 3.5])
    ds = _score(
        tmp_path, verify_fn, fc, obs, "rpss", extra=("--fc-edges", str(pe), "--obs-edges", str(pe)),
        keep=(),
    )  # fmt: skip
    x, y = fc["precip"].values[:, :, 0, 0], obs["precip"].values[:, 0, 0]
    cdf = np.stack([(x < e).mean(0) for e in (1.5, 3.5)], -1)
    ocdf = np.stack([(y < e) for e in (1.5, 3.5)], -1).astype(float)
    rps = ((cdf - ocdf) ** 2).sum(-1)
    clim = ((np.array([1 / 3, 2 / 3]) - ocdf) ** 2).sum(-1)
    assert float(ds["rpss"]) == pytest.approx(1 - rps.mean() / clim.mean(), rel=1e-5)


def test_reliability_and_roc_curves(tmp_path, verify_fn):
    rng = np.random.default_rng(8)
    prob = make_gridded(n_time=40, name="p")
    prob["p"].values = rng.uniform(0, 1, prob["p"].shape)
    prob["p"].attrs = {"units": "1"}
    event = prob.copy(deep=True)
    event["p"].values = (rng.uniform(0, 1, prob["p"].shape) < prob["p"].values).astype(float)
    ds = _score(
        tmp_path, verify_fn, prob, event, "reliability", "roc",
        extra=("--threshold", "1", "--probability"), keep=(),
    )  # fmt: skip
    p, o = prob["p"].values.ravel(), event["p"].values.ravel()
    k = np.clip(np.floor(p * 10 + 1e-6), 0, 9)
    assert float(ds["sample_fraction"].sum()) == pytest.approx(1.0, rel=1e-5)
    assert float(ds["observed_frequency"][3]) == pytest.approx(o[k == 3].mean(), rel=1e-5)
    assert float(ds["forecast_probability"][3]) == pytest.approx(p[k == 3].mean(), rel=1e-5)
    hit = [((p >= t) & (o == 1)).sum() / (o == 1).sum() for t in np.arange(0.1, 1.01, 0.1)]
    np.testing.assert_allclose(ds["hit_rate"].values, hit, rtol=1e-5)
    assert 0.6 < float(ds["roc_area"]) <= 1.0


def test_spread_skill(tmp_path, verify_fn):
    rng = np.random.default_rng(9)
    fc = xr.concat([make_gridded(n_time=30) for _ in range(5)], dim="number")
    fc["precip"].values = rng.normal(5, 1, fc["precip"].shape)
    obs = make_gridded(n_time=30)
    obs["precip"].values = rng.normal(5, 1, obs["precip"].shape)
    ds = _score(tmp_path, verify_fn, fc, obs, "spread", keep=())
    x, y = fc["precip"].values, obs["precip"].values
    var = (x.var(0, ddof=1) * 6 / 5).mean()
    assert float(ds["spread"]) == pytest.approx(
        np.sqrt(var / ((x.mean(0) - y) ** 2).mean()), rel=1e-4
    )


def test_by_label_pools_each_group(tmp_path, verify_fn):
    fc, obs = _random_pair(10)
    fc = fc.assign_coords(zone=("latitude", ["n", "n", "s"]))
    ds = _score(
        tmp_path, verify_fn, fc, obs, "rmse",
        extra=("--by", "zone"), keep=(),
    )  # fmt: skip
    err = fc["precip"].values - obs["precip"].values
    assert ds["rmse"].dims == ("zone",)
    assert float(ds["rmse"].sel(zone="n")) == pytest.approx(
        np.sqrt((err[:, :2] ** 2).mean()), rel=1e-5
    )
    assert int(ds["n_valid"].sel(zone="s")) == err[:, 2].size
    assert "(grouped by zone)" in ds["rmse"].attrs["cell_methods"]


def test_lat_weighted_pooling(tmp_path, verify_fn):
    fc, obs = _random_pair(11, lats=(0.0, 60.0))
    ds = _score(tmp_path, verify_fn, fc, obs, "mae", extra=("--lat-weighted",), keep=())
    err = np.abs(fc["precip"].values - obs["precip"].values)
    w = np.cos(np.deg2rad([0.0, 60.0]))[None, :, None] * np.ones(err.shape)
    assert float(ds["mae"]) == pytest.approx((w * err).sum() / w.sum(), rel=1e-5)
    assert "(latitude-weighted)" in ds["mae"].attrs["cell_methods"]


def test_grouping_flags_are_checked(tmp_path, verify_fn, capsys):
    fc, obs = _random_pair(12)
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "mae", keep=("step",))
    assert "--keep 'step'" in capsys.readouterr().err
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "mae", extra=("--by", "admin_1"), keep=())
    assert "label-group" in capsys.readouterr().err


def test_several_metrics_in_one_call(tmp_path, verify_fn):
    fc, obs = _random_pair(13)
    metrics = ("rmse", "pearson", "pod", "csi")
    ds = _score(tmp_path, verify_fn, fc, obs, *metrics, extra=("--threshold", "4"), keep=())
    assert set(ds.data_vars) == {*metrics, "n_valid"}
    err = fc["precip"].values - obs["precip"].values
    assert float(ds["rmse"]) == pytest.approx(np.sqrt((err**2).mean()), rel=1e-5)
    f, o = fc["precip"].values >= 4, obs["precip"].values >= 4
    assert float(ds["pod"]) == pytest.approx((f & o).sum() / o.sum(), rel=1e-5)


# --- edge cases ---------------------------------------------------------------------------


def test_partial_windows_are_not_scored(tmp_path, verify_fn):
    """A window with aggregation_coverage < 1 is left out, never scored as complete."""
    fc, obs = make_gridded(n_time=3, fill=3.0), make_gridded(n_time=3, fill=1.0)
    obs = obs.assign_coords(aggregation_coverage=("time", [1.0, 0.5, 1.0]))
    mae = _score(tmp_path, verify_fn, fc, obs, "mae")["mae"]
    assert mae.isel(time=1).isnull().all()
    assert mae.isel(time=[0, 2]).notnull().all()


def test_mismatched_aggregation_periods_are_refused(tmp_path, verify_fn, capsys):
    fc, obs = make_gridded(n_time=2, fill=3.0), make_gridded(n_time=2, fill=1.0)
    fc["precip"].attrs["aggregation_period"] = "3 days"
    obs["precip"].attrs["aggregation_period"] = "1 day"
    _expect_exit(_score, tmp_path, verify_fn, fc, obs, "mae")
    assert "aggregate-temporal both" in capsys.readouterr().err
    obs["precip"].attrs["aggregation_period"] = "3D"  # same span, other spelling
    assert _score(tmp_path, verify_fn, fc, obs, "mae")["mae"].notnull().all()


def test_static_mask_applies_to_every_lead(tmp_path, verify_fn):
    fc = make_forecast(n_step=2, name="precip", fill=3.0)
    fc["precip"].attrs["units"] = "mm day-1"
    obs = make_gridded(n_time=2, fill=1.0)
    land = make_gridded(n_time=1, fill=1.0, name="land").isel(time=0, drop=True)
    land["land"].attrs = {"units": "1"}
    land["land"].values[0, 0] = 0.0
    pm = write_zarr(land, tmp_path / "land.zarr")
    mae = _score(tmp_path, verify_fn, fc, obs, "mae", extra=("--filter", str(pm)))["mae"]
    assert mae.isel(latitude=0, longitude=0).isnull().all()
    assert int(mae.notnull().sum()) == 2 * 3


def test_noleap_forecast_leads(tmp_path, verify_fn):
    cftime = pytest.importorskip("cftime")
    fc = make_forecast(n_step=2, name="precip", fill=3.0)
    fc["precip"].attrs["units"] = "mm day-1"
    fc = fc.assign_coords(time=cftime.DatetimeNoLeap(2026, 1, 1))
    obs = make_gridded(n_time=2, fill=1.0).convert_calendar("noleap")
    bias = _score(tmp_path, verify_fn, fc, obs, "bias")["bias"]
    np.testing.assert_allclose(bias.values, 2.0)


def test_quintile_forecast_without_edges_is_refused(tmp_path, verify_fn, capsys):
    fc = make_gridded(n_time=1, fill=0.2).expand_dims(quintile=5).copy(deep=True)
    fc["precip"].attrs = {"units": "1"}
    _expect_exit(_score, tmp_path, verify_fn, fc, make_gridded(n_time=1), "rps")
    assert "quintile_threshold" in capsys.readouterr().err


def test_rps_puts_a_value_on_an_edge_in_the_upper_category(tmp_path, verify_fn):
    """Members below the lower edge, obs on it: the obs is in the middle category, so
    rps = 1 and rps_clim = 2/9, and rpss = 1 - 9/2."""
    fc = xr.concat([make_gridded(n_time=1, fill=1.0) for _ in range(3)], dim="number")
    obs = make_gridded(n_time=1, fill=1.5)
    pe = _edges(tmp_path, [1.5, 3.5], lats=(1.0, 2.0), lons=(10.0, 11.0))
    ds = _score(
        tmp_path, verify_fn, fc, obs, "rpss", extra=("--fc-edges", str(pe), "--obs-edges", str(pe))
    )
    np.testing.assert_allclose(ds["rpss"].values, 1 - 9 / 2, rtol=1e-5)


def test_bss_is_nan_where_no_event_was_observed(tmp_path, verify_fn):
    fc, obs = make_gridded(n_time=4, fill=5.0), make_gridded(n_time=4, fill=0.0)
    obs["precip"].values[:, 0, 0] = 5.0  # only one cell sees events
    ds = _score(
        tmp_path,
        verify_fn,
        fc,
        obs,
        "bss",
        extra=("--threshold", "1"),
        keep=("latitude", "longitude"),
    )
    assert np.isnan(ds["bss"].values[1:, :]).all() and np.isnan(ds["bss"].values[0, 1:]).all()
    assert not np.isinf(ds["bss"].values).any()


def test_roc_counts_float32_probabilities_on_a_level(tmp_path, verify_fn):
    prob = make_gridded(n_time=2, fill=0.7, name="p")
    prob["p"] = prob["p"].astype("float32")
    prob["p"].attrs = {"units": "1"}
    event = make_gridded(n_time=2, fill=1.0, name="p")
    event["p"].attrs = {"units": "1"}
    ds = _score(
        tmp_path, verify_fn, prob, event, "roc", "reliability",
        extra=("--threshold", "1", "--probability"), keep=(),
    )  # fmt: skip
    assert float(ds["hit_rate"].sel(probability_threshold=0.7)) == 1
    assert float(ds["sample_fraction"].sel(probability_bin=0.75)) == 1

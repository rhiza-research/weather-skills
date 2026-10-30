"""Correctness tests for mme-fit."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fit_fn():
    return load_skill("mme-fit", "mme_fit").mme_fit


STEPS = np.array([0, 7, 14, 21], dtype="timedelta64[D]")


def _inits(n, start="2000-01-01", every=28, hour=0):
    # Inits 28 days apart with 0-21 day steps never share a valid date, so every
    # (init, step) sample gets its own obs value.
    base = np.datetime64(start, "ns") + np.timedelta64(hour, "h")
    return base + np.arange(n) * np.timedelta64(every, "D")


def _stations(n):
    return [f"S{i:02d}" for i in range(n)]


def _station_forecast(values, inits, stations, name="tp", units="mm day-1", steps=STEPS):
    ds = xr.Dataset(
        {name: (("time", "step", "station_id"), values)},
        coords={
            "time": inits,
            "step": steps.astype("timedelta64[ns]"),
            "station_id": stations,
            "latitude": ("station_id", np.linspace(-1, 1, len(stations))),
            "longitude": ("station_id", np.linspace(36, 37, len(stations))),
        },
    )
    ds[name].attrs["units"] = units
    return ds


def _station_obs(samples, inits, stations, name="precip", units="mm day-1", steps=STEPS):
    """Daily station obs holding each (init, step, station) sample at its valid date."""
    valid = (inits[:, None] + steps.astype("timedelta64[ns]")[None, :]).astype("datetime64[D]")
    days = np.arange(valid.min(), valid.max() + np.timedelta64(1, "D"))
    data = np.full((len(days), len(stations)), np.nan)
    data[np.searchsorted(days, valid.ravel())] = samples.reshape(-1, len(stations))
    ds = xr.Dataset(
        {name: (("time", "station_id"), data)},
        coords={"time": days.astype("datetime64[ns]"), "station_id": stations},
    )
    ds[name].attrs["units"] = units
    return ds


def _logistic_case(rng, n_init=200, n_station=25, true=(-0.5, 1.2, -0.8)):
    shape = (n_init, len(STEPS), n_station)
    a, b = rng.normal(size=shape), rng.normal(size=shape)
    eta = true[0] + true[1] * a + true[2] * b
    event = (rng.random(shape) < 1 / (1 + np.exp(-eta))).astype(float)
    return a, b, event


def _write_case(tmp_path, a, b, obs_values, units="1"):
    inits, stations = _inits(a.shape[0]), _stations(a.shape[2])
    fa = write_zarr(_station_forecast(a, inits, stations, units=units), tmp_path / "aifs.zarr")
    fb = write_zarr(_station_forecast(b, inits, stations, units=units), tmp_path / "ngcm.zarr")
    obs = write_zarr(_station_obs(obs_values, inits, stations, units=units), tmp_path / "obs.zarr")
    return fa, fb, obs


def _fit(fit_fn, tmp_path, fa, fb, obs, *args):
    out = tmp_path / "weights.zarr"
    run_skill(
        fit_fn, "--forecast", str(fa), "--forecast", str(fb), "--obs", str(obs),
        "-o", str(out), *args,
    )  # fmt: skip
    return xr.open_zarr(out, consolidated=True).load(), out


def test_logistic_recovers_weights_and_matches_sklearn(tmp_path, fit_fn):
    rng = np.random.default_rng(0)
    a, b, event = _logistic_case(rng)
    fa, fb, obs = _write_case(tmp_path, a, b, event)

    w, out = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", "logistic")

    assert list(w["predictor"].values) == ["aifs", "ngcm"]
    assert w["coef"].dims == ("predictor",)
    assert float(w["intercept"]) == pytest.approx(-0.5, abs=0.1)
    np.testing.assert_allclose(w["coef"].values, [1.2, -0.8], atol=0.1)
    assert int(w["n_samples"]) == a.size
    assert w.attrs["model"] == "logistic"
    assert load_history(out)[-1]["skill"] == "mme-fit"

    X = np.column_stack([a.ravel(), b.ravel()])
    ref = LogisticRegression(C=np.inf, tol=1e-12, max_iter=1000).fit(X, event.ravel())
    np.testing.assert_allclose(w["coef"].values, ref.coef_[0], atol=1e-6)
    assert float(w["intercept"]) == pytest.approx(ref.intercept_[0], abs=1e-6)


def test_logistic_alpha_matches_sklearn_c(tmp_path, fit_fn):
    rng = np.random.default_rng(1)
    a, b, event = _logistic_case(rng, n_init=40, n_station=5)
    fa, fb, obs = _write_case(tmp_path, a, b, event)

    w, _ = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", "logistic", "--alpha", "50")

    X = np.column_stack([a.ravel(), b.ravel()])
    ref = LogisticRegression(C=1 / 50, tol=1e-12, max_iter=1000).fit(X, event.ravel())
    np.testing.assert_allclose(w["coef"].values, ref.coef_[0], atol=1e-6)
    assert float(w["intercept"]) == pytest.approx(ref.intercept_[0], abs=1e-6)


@pytest.mark.parametrize(
    ("model", "alpha", "ref"),
    [
        ("linear", 0.0, LinearRegression()),
        ("linear", 3.0, Ridge(alpha=3.0)),
        ("weighted", 0.0, LinearRegression(fit_intercept=False)),
        ("weighted", 3.0, Ridge(alpha=3.0, fit_intercept=False)),
    ],
)
def test_least_squares_match_sklearn(tmp_path, fit_fn, model, alpha, ref):
    rng = np.random.default_rng(2)
    shape = (30, len(STEPS), 4)
    a, b = rng.gamma(2, 3, size=shape), rng.gamma(2, 3, size=shape)
    truth = 1.0 + 0.7 * a + 0.2 * b + rng.normal(size=shape)
    fa, fb, obs = _write_case(tmp_path, a, b, truth, units="mm day-1")

    w, _ = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", model, "--alpha", str(alpha))

    ref.fit(np.column_stack([a.ravel(), b.ravel()]), truth.ravel())
    np.testing.assert_allclose(w["coef"].values, ref.coef_, rtol=1e-8, atol=1e-10)
    if model == "linear":
        assert float(w["intercept"]) == pytest.approx(ref.intercept_, abs=1e-8)
        assert w["intercept"].attrs["units"] == "mm day-1"
    else:
        assert "intercept" not in w
    assert w.attrs["predictor_units"] == "mm day-1"


def test_fit_along_step_gives_one_fit_per_lead(tmp_path, fit_fn):
    rng = np.random.default_rng(3)
    shape = (300, len(STEPS), 20)
    a, b = rng.normal(size=shape), rng.normal(size=shape)
    slope = np.array([1.5, 1.0, 0.5, 0.0])[None, :, None]
    eta = -0.2 + slope * a + 0.3 * b
    event = (rng.random(shape) < 1 / (1 + np.exp(-eta))).astype(float)
    fa, fb, obs = _write_case(tmp_path, a, b, event)

    w, _ = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", "logistic", "--fit-along", "step")

    assert w["coef"].dims == ("step", "predictor")
    assert w["intercept"].dims == ("step",)
    np.testing.assert_allclose(
        w["coef"].sel(predictor="aifs").values, [1.5, 1.0, 0.5, 0.0], atol=0.12
    )
    assert (w["n_samples"].values == 300 * 20).all()
    assert (w["n_iter"].values < 100).all()


def test_fit_along_station_keeps_station_coords(tmp_path, fit_fn):
    rng = np.random.default_rng(4)
    shape = (60, len(STEPS), 3)
    a, b = rng.normal(size=shape), rng.normal(size=shape)
    truth = 2 * a - b
    fa, fb, obs = _write_case(tmp_path, a, b, truth, units="mm day-1")

    w, _ = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", "weighted", "--fit-along", "station_id")

    assert w["coef"].dims == ("station_id", "predictor")
    assert list(w["station_id"].values) == ["S00", "S01", "S02"]
    assert "latitude" in w.coords and w["latitude"].dims == ("station_id",)
    np.testing.assert_allclose(w["coef"].values, [[2, -1]] * 3, atol=1e-9)


def test_obs_matched_at_init_plus_step_on_calendar_day(tmp_path, fit_fn):
    # 12 UTC inits against daily obs: the lookup drops the hour. The forecast equals
    # the obs at its valid date, so a perfect linear fit comes back.
    rng = np.random.default_rng(5)
    stations = _stations(2)
    days = np.arange(np.datetime64("2026-01-01"), np.datetime64("2026-03-01"))
    obs_vals = rng.gamma(2, 2, size=(len(days), 2))
    obs = xr.Dataset(
        {"precip": (("time", "station_id"), obs_vals)},
        coords={"time": days.astype("datetime64[ns]"), "station_id": stations},
    )
    obs["precip"].attrs["units"] = "mm day-1"
    inits = _inits(6, start="2026-01-01", every=5, hour=12)
    steps = np.arange(10).astype("timedelta64[D]")
    day_idx = ((inits - np.datetime64("2026-01-01", "ns")) // np.timedelta64(1, "D"))[
        :, None
    ] + np.arange(10)
    fc = obs_vals[day_idx]
    noise = fc + rng.normal(scale=0.1, size=fc.shape)
    fa = write_zarr(_station_forecast(fc, inits, stations, steps=steps), tmp_path / "aifs.zarr")
    fb = write_zarr(_station_forecast(noise, inits, stations, steps=steps), tmp_path / "ngcm.zarr")
    obs_path = write_zarr(obs, tmp_path / "obs.zarr")

    w, _ = _fit(fit_fn, tmp_path, fa, fb, obs_path, "--model", "linear")

    np.testing.assert_allclose(w["coef"].values, [1.0, 0.0], atol=1e-9)
    assert float(w["intercept"]) == pytest.approx(0.0, abs=1e-9)
    assert int(w["n_samples"]) == 6 * 10 * 2


def test_missing_values_are_dropped_not_filled(tmp_path, fit_fn):
    rng = np.random.default_rng(6)
    shape = (20, len(STEPS), 3)
    a, b = rng.normal(size=shape), rng.normal(size=shape)
    truth = 0.5 * a + 0.5 * b
    a[0, 0, 0] = np.nan
    truth[1, 1, 1] = np.nan
    fa, fb, obs = _write_case(tmp_path, a, b, truth, units="mm day-1")

    w, _ = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", "weighted")

    assert int(w["n_samples"]) == a.size - 2
    np.testing.assert_allclose(w["coef"].values, [0.5, 0.5], atol=1e-9)


def test_interaction_and_names(tmp_path, fit_fn):
    rng = np.random.default_rng(7)
    shape = (40, len(STEPS), 5)
    a, b = rng.normal(size=shape), rng.normal(size=shape)
    truth = 1 + a + 2 * b + 3 * a * b
    fa, fb, obs = _write_case(tmp_path, a, b, truth, units="1")

    w, _ = _fit(
        fit_fn, tmp_path, fa, fb, obs, "--model", "linear", "--names", "x,y",
        "--interaction", "x:y",
    )  # fmt: skip

    assert list(w["predictor"].values) == ["x", "y", "x:y"]
    np.testing.assert_allclose(w["coef"].values, [1, 2, 3], atol=1e-9)


def test_stations_missing_from_an_input_are_reported(tmp_path, fit_fn, capsys):
    rng = np.random.default_rng(8)
    inits = _inits(10)
    a = rng.normal(size=(10, len(STEPS), 4))
    fa = write_zarr(_station_forecast(a, inits, ["S0", "S1", "S2", "S3"]), tmp_path / "aifs.zarr")
    fb = write_zarr(_station_forecast(a, inits, ["S0", "S1", "S2", "S3"]), tmp_path / "ngcm.zarr")
    obs = write_zarr(_station_obs(a, inits, ["S1", "S2", "S3", "S4"]), tmp_path / "obs.zarr")

    w, _ = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", "linear", "--alpha", "1")

    err = capsys.readouterr().err
    assert "2 station_id not in every input" in err
    assert "S0" in err and "S4" in err
    assert int(w["n_samples"]) == 10 * len(STEPS) * 3


@pytest.mark.parametrize("case", ["period", "layout", "spacing", "logistic-target", "no-match"])
def test_usage_errors(tmp_path, fit_fn, case):
    rng = np.random.default_rng(9)
    a = rng.normal(size=(5, len(STEPS), 2))
    inits, stations = _inits(5), _stations(2)
    fc = _station_forecast(a, inits, stations)
    obs = _station_obs(a, inits, stations)
    model = "linear"
    if case == "period":
        fc["tp"].attrs["aggregation_period"] = "7 day"
        obs["precip"].attrs["aggregation_period"] = "1 day"
    elif case == "layout":
        lat, lon = [0.0, 1.0], [36.0, 37.0]
        fc = xr.Dataset(
            {"tp": (("time", "step", "latitude", "longitude"), np.zeros((5, 4, 2, 2)))},
            coords={"time": inits, "step": STEPS.astype("timedelta64[ns]"),
                    "latitude": lat, "longitude": lon},
        )  # fmt: skip
        fc["tp"].attrs["units"] = "mm day-1"
    elif case == "spacing":
        grid = {"time": obs["time"].values, "latitude": [0.0, 1.0], "longitude": [36.0, 37.0]}
        shape = (obs.sizes["time"], 2, 2)
        obs = xr.Dataset({"precip": (("time", "latitude", "longitude"), np.zeros(shape))})
        obs = obs.assign_coords(grid)
        obs["precip"].attrs["units"] = "mm day-1"
        fine = {"latitude": [0.0, 0.5], "longitude": [36.0, 36.5]}
        fc = xr.Dataset(
            {"tp": (("time", "step", "latitude", "longitude"), np.zeros((5, 4, 2, 2)))},
            coords={"time": inits, "step": STEPS.astype("timedelta64[ns]"), **fine},
        )
        fc["tp"].attrs["units"] = "mm day-1"
    elif case == "logistic-target":
        model = "logistic"
    else:
        obs = obs.assign_coords(time=obs["time"] + np.timedelta64(3650, "D"))
    fa = write_zarr(fc, tmp_path / "aifs.zarr")
    o = write_zarr(obs, tmp_path / "obs.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fit_fn, "--forecast", str(fa), "--obs", str(o), "--model", model,
            "-o", str(tmp_path / "w.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_separated_logistic_warns_and_flags_n_iter(tmp_path, fit_fn):
    rng = np.random.default_rng(10)
    shape = (20, len(STEPS), 3)
    a, b = rng.normal(size=shape), rng.normal(size=shape)
    event = (a > 0).astype(float)
    fa, fb, obs = _write_case(tmp_path, a, b, event)

    with pytest.warns(UserWarning, match="did not converge"):
        w, _ = _fit(fit_fn, tmp_path, fa, fb, obs, "--model", "logistic", "--max-iter", "15")

    assert int(w["n_iter"]) == 15
    assert float(w["coef"].sel(predictor="aifs")) > 5

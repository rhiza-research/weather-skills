"""Correctness tests for mme-blend."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def blend_fn():
    return load_skill("mme-blend", "mme_blend").mme_blend


@pytest.fixture(scope="module")
def fit_fn():
    return load_skill("mme-fit", "mme_fit").mme_fit


def _forecast(rng, units="mm day-1", scale=1.0, members=None):
    ds = make_forecast(n_step=4, lats=(1.0, 2.0, 3.0), lons=(10.0, 11.0), members=members)
    ds["tp"].values = rng.gamma(2, 3, size=ds["tp"].shape) * scale
    ds["tp"].attrs["units"] = units
    return ds


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


def _blend(blend_fn, tmp_path, *args):
    out = tmp_path / "blend.zarr"
    run_skill(blend_fn, *args, "-o", str(out))
    return xr.open_zarr(out, consolidated=True).load(), out


def test_fixed_weights_convert_units_and_ensemble_mean(tmp_path, blend_fn):
    rng = np.random.default_rng(0)
    a = _forecast(rng, members=3)
    b = _forecast(rng)
    b_hourly = b.copy(deep=True)
    b_hourly["tp"].values = b["tp"].values / 24
    b_hourly["tp"].attrs["units"] = "mm h-1"
    fa = write_zarr(a, tmp_path / "aifs.zarr")
    fb = write_zarr(b_hourly, tmp_path / "ngcm.zarr")

    ds, out = _blend(
        blend_fn, tmp_path, "--forecast", str(fa), "--forecast", str(fb),
        "--model", "weighted", "--coef", "aifs=0.6", "--coef", "ngcm=0.4",
    )  # fmt: skip

    expect = 0.6 * a["tp"].mean("number").values + 0.4 * b["tp"].values
    np.testing.assert_allclose(ds["blend"].values, expect, rtol=1e-5)
    assert ds["blend"].attrs["units"] == "mm day-1"
    assert ds["blend"].dims == ("step", "latitude", "longitude")
    assert "time" in ds.coords
    assert load_history(out)[-1]["skill"] == "mme-blend"


def test_linear_weights_per_step_with_interaction(tmp_path, blend_fn):
    rng = np.random.default_rng(1)
    a, b = _forecast(rng), _forecast(rng)
    steps = a["step"].values
    weights = xr.Dataset(
        {
            "coef": (("step", "predictor"), rng.normal(size=(4, 3))),
            "intercept": ("step", rng.normal(size=4)),
        },
        coords={"step": steps, "predictor": ["aifs", "ngcm", "aifs:ngcm"]},
        attrs={"model": "linear", "predictor_units": "mm day-1"},
    )
    fa = write_zarr(a, tmp_path / "aifs.zarr")
    fb = write_zarr(b, tmp_path / "ngcm.zarr")
    w = write_zarr(weights, tmp_path / "w.zarr")

    ds, _ = _blend(
        blend_fn, tmp_path, "--forecast", str(fa), "--forecast", str(fb), "--weights", str(w)
    )

    av, bv = a["tp"].values, b["tp"].values
    c = weights["coef"].values[:, :, None, None]
    expect = (
        weights["intercept"].values[:, None, None] + c[:, 0] * av + c[:, 1] * bv + c[:, 2] * av * bv
    )
    np.testing.assert_allclose(ds["blend"].values, expect, rtol=1e-5)
    assert ds["blend"].attrs["units"] == "mm day-1"


def test_logistic_pooled_weights_on_a_grid_and_nan_stays_nan(tmp_path, blend_fn):
    rng = np.random.default_rng(2)
    a, b = _forecast(rng), _forecast(rng)
    a["tp"].values[0, 0, 0] = np.nan
    weights = xr.Dataset(
        {"coef": ("predictor", [0.3, -0.2]), "intercept": ((), -1.0)},
        coords={"predictor": ["aifs", "ngcm"]},
        attrs={"model": "logistic"},
    )
    fa = write_zarr(a, tmp_path / "aifs.zarr")
    fb = write_zarr(b, tmp_path / "ngcm.zarr")
    w = write_zarr(weights, tmp_path / "w.zarr")

    ds, _ = _blend(
        blend_fn, tmp_path, "--forecast", str(fa), "--forecast", str(fb), "--weights", str(w)
    )

    expect = _sigmoid(-1.0 + 0.3 * a["tp"].values - 0.2 * b["tp"].values)
    np.testing.assert_allclose(ds["probability"].values, expect, rtol=1e-5)
    assert ds["probability"].attrs["units"] == "1"
    assert np.isnan(ds["probability"].values[0, 0, 0])


def test_fit_then_blend_round_trip_at_stations(tmp_path, fit_fn, blend_fn):
    rng = np.random.default_rng(3)
    stations = [f"S{i}" for i in range(6)]
    inits = np.datetime64("2001-01-01", "ns") + np.arange(80) * np.timedelta64(28, "D")
    steps = np.array([0, 7, 14, 21], dtype="timedelta64[D]").astype("timedelta64[ns]")
    shape = (80, 4, 6)
    a, b = rng.normal(size=shape), rng.normal(size=shape)
    event = (rng.random(shape) < _sigmoid(0.4 + a - b)).astype(float)

    def station_fc(values):
        ds = xr.Dataset(
            {"probability": (("time", "step", "station_id"), values)},
            coords={
                "time": inits, "step": steps, "station_id": stations,
                "latitude": ("station_id", np.linspace(-1, 1, 6)),
                "longitude": ("station_id", np.linspace(36, 37, 6)),
            },
        )  # fmt: skip
        ds["probability"].attrs["units"] = "1"
        return ds

    valid = (inits[:, None] + steps[None, :]).astype("datetime64[D]").ravel()
    days = np.arange(valid.min(), valid.max() + np.timedelta64(1, "D"))
    obs_vals = np.full((len(days), 6), np.nan)
    obs_vals[np.searchsorted(days, valid)] = event.reshape(-1, 6)
    obs = xr.Dataset(
        {"indicator": (("time", "station_id"), obs_vals)},
        coords={"time": days.astype("datetime64[ns]"), "station_id": stations},
    )
    obs["indicator"].attrs["units"] = "1"
    fa = write_zarr(station_fc(a), tmp_path / "aifs.zarr")
    fb = write_zarr(station_fc(b), tmp_path / "ngcm.zarr")
    o = write_zarr(obs, tmp_path / "obs.zarr")
    w = tmp_path / "w.zarr"

    run_skill(
        fit_fn, "--forecast", str(fa), "--forecast", str(fb), "--obs", str(o),
        "--model", "logistic", "--fit-along", "station_id", "-o", str(w),
    )  # fmt: skip
    ds, _ = _blend(
        blend_fn, tmp_path, "--forecast", str(fa), "--forecast", str(fb), "--weights", str(w)
    )

    weights = xr.open_zarr(w, consolidated=True).load()
    coef = weights["coef"].values
    expect = _sigmoid(weights["intercept"].values + coef[:, 0] * a + coef[:, 1] * b)
    got = ds["probability"].transpose("time", "step", "station_id").values
    np.testing.assert_allclose(got, expect, rtol=1e-5)
    assert ds["latitude"].dims == ("station_id",)


@pytest.mark.parametrize(
    "args",
    [
        ["--model", "weighted", "--coef", "aifs=1", "--coef", "other=1"],  # name mismatch
        ["--coef", "aifs=1", "--coef", "ngcm=1"],  # --coef without --model
        ["--model", "weighted", "--coef", "aifs=1", "--coef", "ngcm=1", "--intercept", "2"],
        ["--model", "linear", "--coef", "aifs"],  # malformed
        [],  # neither --weights nor --coef
    ],
)
def test_usage_errors(tmp_path, blend_fn, args):
    rng = np.random.default_rng(4)
    fa = write_zarr(_forecast(rng), tmp_path / "aifs.zarr")
    fb = write_zarr(_forecast(rng), tmp_path / "ngcm.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            blend_fn, "--forecast", str(fa), "--forecast", str(fb), *args,
            "-o", str(tmp_path / "b.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_weights_model_mismatch_exits_2(tmp_path, blend_fn):
    rng = np.random.default_rng(5)
    fa = write_zarr(_forecast(rng), tmp_path / "aifs.zarr")
    weights = xr.Dataset(
        {"coef": ("predictor", [1.0])}, coords={"predictor": ["aifs"]}, attrs={"model": "weighted"}
    )
    w = write_zarr(weights, tmp_path / "w.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            blend_fn, "--forecast", str(fa), "--weights", str(w), "--model", "logistic",
            "-o", str(tmp_path / "b.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2

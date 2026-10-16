"""Correctness tests for verify."""

from pathlib import Path

import numpy as np
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

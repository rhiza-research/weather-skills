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


def test_hit_miss_false_alarm_and_correct_negative(tmp_path, verify_fn):
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
    assert hit[0, 1] == pytest.approx(3)  # forecast only: false alarm
    assert hit[1, 0] == pytest.approx(2)  # obs only: miss
    assert hit[1, 1] == pytest.approx(0)
    assert ds["event_hit"].attrs["flag_meanings"] == "correct_negative hit miss false_alarm"
    assert ds["event_hit"].attrs["event_threshold"] == 1.0
    assert ds["event_hit"].attrs["event_variable"] == "precip"
    assert ds.attrs["verify_metric"] == "hits"
    assert ds.attrs["verify_score_summary"] == (
        "hit rate 50%  (1/2 obs events), false alarms 1 (FAR 50%), CSI 0.33"
    )
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


def test_single_init_scores_each_lead_against_its_valid_date(tmp_path, verify_fn, capsys):
    """Step s of a 2026-01-01 init pairs with obs on 2026-01-01 + s, no step-to-time first."""
    fc = make_forecast(n_step=2, fill=5.0, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    fc["precip"].values[1] = 0.0  # lead 1 day forecasts no event
    obs = make_gridded(n_time=3, fill=5.0)
    obs["precip"].values[2] = 0.0  # 2026-01-03 has none; no lead lands there
    ds = _verify(tmp_path, verify_fn, fc, obs, "--threshold", "1")
    hit = ds["event_hit"]
    assert hit.dims[:2] == ("time", "step")
    assert (hit.isel(step=0).values == 1).all()
    assert (hit.isel(step=1).values == 2).all()  # obs 2026-01-02 had rain: a miss
    out = capsys.readouterr().out
    assert "step 0 days 00:00:00: hit rate 100%" in out
    assert "step 1 days 00:00:00: hit rate 0%" in out


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
    assert (ds["event_hit"].values == 3).all()


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


def test_init_window_leads_past_obs_record_stay_nan(tmp_path, verify_fn):
    """Two inits x two leads: valid times without obs are NaN, never filled."""
    fc = make_forecast(n_step=2, name="precip", fill=3.0)
    fc["precip"].attrs["units"] = "mm day-1"
    fc = xr.concat([fc, fc.assign_coords(time=np.datetime64("2026-01-02", "ns"))], dim="time")
    obs = make_gridded(n_time=2, fill=1.0)  # 2026-01-01 and 2026-01-02 only
    bias = _verify(tmp_path, verify_fn, fc, obs, "--metric", "bias")["bias"]
    assert bias.dims[:2] == ("time", "step")
    np.testing.assert_allclose(bias.isel(time=0).values, 2.0)
    np.testing.assert_allclose(bias.isel(time=1, step=0).values, 2.0)
    assert np.isnan(bias.isel(time=1, step=1).values).all()  # 2026-01-03: no obs


def test_leads_with_no_obs_at_any_valid_time_are_refused(tmp_path, verify_fn, capsys):
    fc = make_forecast(n_step=2, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    obs = make_gridded(n_time=2, start="2027-01-01")
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, fc, obs, "--metric", "bias")
    assert exc.value.code == 2
    assert "no --obs time matches a forecast valid time" in capsys.readouterr().err


def test_total_threshold_on_a_rate_uses_its_aggregation_period(tmp_path, verify_fn, capsys):
    """50 mm over 3-day mean rates is 16.67 mm day-1: 17 is an event, 16 is not."""
    fc = make_gridded(n_time=1, fill=17.0)
    fc["precip"].attrs["aggregation_period"] = "3 day"
    fc["precip"].values[0, 0, 0] = 16.0
    obs = make_gridded(n_time=1, fill=17.0)
    ds = _verify(tmp_path, verify_fn, fc, obs, "--threshold", "50 mm")
    assert ds["event_hit"].values[0, 0, 0] == 2
    assert (ds["event_hit"].values.ravel()[1:] == 1).all()
    assert ds["event_hit"].attrs["event_threshold"] == pytest.approx(50 / 3)
    assert "50 mm over 3 day" in capsys.readouterr().err


def test_total_threshold_without_a_period_is_refused(tmp_path, verify_fn):
    fc = make_gridded(n_time=1, fill=17.0)
    for attr in ("aggregation_period", "data_interval"):
        fc["precip"].attrs.pop(attr, None)
    with pytest.raises(SystemExit) as exc:
        _verify(tmp_path, verify_fn, fc, make_gridded(n_time=1), "--threshold", "50 mm")
    assert exc.value.code == 2


def test_member_fraction_counts_members_at_the_threshold(tmp_path, verify_fn):
    """2 of 4 members >= 10: an event at --member-fraction 0.5, not at 0.75."""
    fc = make_forecast(n_step=1, members=4, name="precip")
    fc["precip"].attrs["units"] = "mm day-1"
    # Ensemble mean 6 is below 10; two members reach it.
    fc["precip"].values[:] = np.array([0.0, 4.0, 10.0, 10.0])[:, None, None, None]
    fc = fc.isel(step=0, drop=True).expand_dims(time=[np.datetime64("2026-01-01", "ns")])
    obs = make_gridded(n_time=1, fill=12.0)
    half = _verify(tmp_path, verify_fn, fc, obs, "--threshold", "10", "--member-fraction", "0.5")
    assert (half["event_hit"].values == 1).all()
    assert half["event_hit"].attrs["event_member_fraction"] == 0.5
    most = _verify(tmp_path, verify_fn, fc, obs, "--threshold", "10", "--member-fraction", "0.75")
    assert (most["event_hit"].values == 2).all()
    mean = _verify(tmp_path, verify_fn, fc, obs, "--threshold", "10")
    assert (mean["event_hit"].values == 2).all()


def test_member_fraction_without_members_is_refused(tmp_path, verify_fn):
    with pytest.raises(SystemExit) as exc:
        _verify(
            tmp_path,
            verify_fn,
            make_gridded(n_time=1),
            make_gridded(n_time=1),
            "--member-fraction",
            "0.5",
        )
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

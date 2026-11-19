"""Correctness tests for resample-time."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from weather_skills_core.units import AGGREGATION_COVERAGE_COORD

DAY = np.timedelta64(1, "D")
JAN1 = np.datetime64("2026-01-01", "ns")


@pytest.fixture(scope="module")
def resample():
    return load_skill("resample-time", "resample_time").resample_time


def _samples(spans, values, coverage=None):
    """One rate per [first day, last day] span (1-based January days, inclusive)."""
    starts = np.array([JAN1 + (a - 1) * DAY for a, _ in spans])
    ends = np.array([JAN1 + b * DAY for _, b in spans])
    attrs = {"units": "mm day-1", "standard_name": "lwe_precipitation_rate"}
    ds = xr.Dataset(
        {"precip": (("time", "lat"), np.array(values, float)[:, None], attrs)},
        coords={"time": starts, "lat": [0.0]},
    )
    ds["time_bounds"] = (("time", "nv"), np.stack([starts, ends], axis=1))
    ds["time"].attrs["bounds"] = "time_bounds"
    if coverage is not None:
        ds = ds.assign_coords({AGGREGATION_COVERAGE_COORD: ("time", coverage)})
    return ds


def _run(resample, tmp_path, ds, *args):
    out = tmp_path / "out.zarr"
    run_skill(resample, "-i", str(write_zarr(ds, tmp_path / "in.zarr")), "-o", str(out), *args)
    return xr.open_zarr(out)


def test_overlapping_weeks_average_where_they_overlap(tmp_path, resample):
    """Days 1-7, 2-8, 3-9 and 23-30: each day is the plain mean of the weeks covering it."""
    ds = _samples([(1, 7), (2, 8), (3, 9), (23, 30)], [1, 2, 3, 4])
    out = _run(resample, tmp_path, ds, "--period", "1 day")
    vals = out["precip"].values[:, 0]
    cov = out[AGGREGATION_COVERAGE_COORD].values
    assert out.sizes["time"] == 30
    assert out["time"].values[0] == JAN1
    np.testing.assert_allclose(vals[:9], [1, 1.5, 2, 2, 2, 2, 2, 2.5, 3])
    assert np.isnan(vals[9:22]).all() and (cov[9:22] == 0).all()
    np.testing.assert_allclose(vals[22:], 4)
    assert cov[:9].tolist() == [1] * 9 and cov[22:].tolist() == [1] * 8
    attrs = out["precip"].attrs
    assert attrs["aggregation_period"] == "1 day" and attrs["data_interval"] == "1 day"
    assert "overlapping samples" in attrs["cell_methods"]
    assert "time_bounds" not in out


def test_coverage_is_mean_of_covering_samples(tmp_path, resample):
    """A day covered by a complete week and a 3/7 week is 5/7 complete."""
    ds = _samples([(1, 7), (2, 8)], [1, 2], coverage=[1.0, 3 / 7])
    cov = _run(resample, tmp_path, ds, "--period", "1 day")[AGGREGATION_COVERAGE_COORD].values
    np.testing.assert_allclose(cov, [1] + [5 / 7] * 6 + [3 / 7])


def test_interval_from_data_interval_without_bounds(tmp_path, resample):
    """A weekly mean labeled at each init, inits 2 days apart, overlaps by its data_interval."""
    times = JAN1 + np.arange(3) * 2 * DAY
    attrs = {"units": "mm day-1", "data_interval": "7 day"}
    ds = xr.Dataset({"precip": (("time",), [1.0, 2.0, 3.0], attrs)}, coords={"time": times})
    out = _run(resample, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["precip"].values, [1, 1, 1.5, 1.5, 2, 2, 2, 2.5, 2.5, 3, 3])


def test_sub_daily_period(tmp_path, resample):
    """Two overlapping 30-minute means on a 15-minute grid."""
    t0 = np.datetime64("2026-01-01T06:00", "ns")
    attrs = {"units": "mm hr-1", "data_interval": "30 minute"}
    ds = xr.Dataset(
        {"precip": (("time",), [2.0, 4.0], attrs)},
        coords={"time": [t0, t0 + np.timedelta64(15, "m")]},
    )
    out = _run(resample, tmp_path, ds, "--period", "15 minute")
    assert out["time"].values[0] == t0
    np.testing.assert_allclose(out["precip"].values, [2, 3, 4])
    assert out["precip"].attrs["aggregation_period"] == "15 minute"


def test_step_axis(tmp_path, resample):
    """Overlapping lead windows on step resample from lead 0."""
    attrs = {"units": "mm day-1", "data_interval": "2 day"}
    ds = xr.Dataset(
        {"precip": (("step",), [1.0, 3.0], attrs)},
        coords={"step": np.array([0, 1]) * DAY},
    )
    out = _run(resample, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["precip"].values, [1, 2, 3])
    assert out["step"].values.tolist() == (np.arange(3) * DAY).astype("m8[ns]").tolist()


def test_cell_missing_a_sample_others_have_is_nan(tmp_path, resample):
    """A cell with one of two overlapping samples is not averaged over what it has."""
    ds = _samples([(1, 2), (2, 3)], [1, 2])
    ds = ds.reindex(lat=[0.0, 1.0], fill_value=1.0)
    ds["precip"][1, 1] = np.nan
    vals = _run(resample, tmp_path, ds, "--period", "1 day")["precip"].values
    np.testing.assert_allclose(vals[:, 0], [1, 1.5, 2])
    assert vals[0, 1] == 1 and np.isnan(vals[1, 1]) and np.isnan(vals[2, 1])


def test_period_off_sample_edges_suggests_one(tmp_path, resample, capsys):
    ds = _samples([(1, 7), (2, 8)], [1, 2])
    with pytest.raises(SystemExit) as exc:
        _run(resample, tmp_path, ds, "--period", "7 day")
    assert exc.value.code == 2
    assert "1 day" in capsys.readouterr().err


def test_no_interval_exits_2(tmp_path, resample):
    ds = xr.Dataset(
        {"precip": (("time",), [1.0, 2.0], {"units": "mm day-1"})},
        coords={"time": [JAN1, JAN1 + DAY]},
    )
    with pytest.raises(SystemExit) as exc:
        _run(resample, tmp_path, ds, "--period", "1 day")
    assert exc.value.code == 2

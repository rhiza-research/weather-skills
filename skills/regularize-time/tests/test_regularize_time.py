"""Correctness tests for regularize-time."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from weather_skills_core.units import AGGREGATION_COVERAGE_COORD

DAY = np.timedelta64(1, "D")
JAN1 = np.datetime64("2026-01-01", "ns")


@pytest.fixture(scope="module")
def regularize():
    return load_skill("regularize-time", "regularize_time").regularize_time


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


def _run(regularize, tmp_path, ds, *args):
    out = tmp_path / "out.zarr"
    run_skill(regularize, "-i", str(write_zarr(ds, tmp_path / "in.zarr")), "-o", str(out), *args)
    return xr.open_zarr(out)


def test_overlapping_weeks_average_where_they_overlap(tmp_path, regularize):
    """Days 1-7, 2-8, 3-9 and 23-30: each day is the plain mean of the weeks covering it."""
    ds = _samples([(1, 7), (2, 8), (3, 9), (23, 30)], [1, 2, 3, 4])
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
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


def test_coverage_is_mean_of_covering_samples(tmp_path, regularize):
    """A day covered by a complete week and a 3/7 week is 5/7 complete."""
    ds = _samples([(1, 7), (2, 8)], [1, 2], coverage=[1.0, 3 / 7])
    cov = _run(regularize, tmp_path, ds, "--period", "1 day")[AGGREGATION_COVERAGE_COORD].values
    np.testing.assert_allclose(cov, [1] + [5 / 7] * 6 + [3 / 7])


def test_interval_from_data_interval_without_bounds(tmp_path, regularize):
    """A weekly mean labeled at each init, inits 2 days apart, overlaps by its data_interval."""
    times = JAN1 + np.arange(3) * 2 * DAY
    attrs = {"units": "mm day-1", "data_interval": "7 day"}
    ds = xr.Dataset({"precip": (("time",), [1.0, 2.0, 3.0], attrs)}, coords={"time": times})
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["precip"].values, [1, 1, 1.5, 1.5, 2, 2, 2, 2.5, 2.5, 3, 3])


def test_sub_daily_period(tmp_path, regularize):
    """Two overlapping 30-minute means on a 15-minute grid."""
    t0 = np.datetime64("2026-01-01T06:00", "ns")
    attrs = {"units": "mm hr-1", "data_interval": "30 minute"}
    ds = xr.Dataset(
        {"precip": (("time",), [2.0, 4.0], attrs)},
        coords={"time": [t0, t0 + np.timedelta64(15, "m")]},
    )
    out = _run(regularize, tmp_path, ds, "--period", "15 minute")
    assert out["time"].values[0] == t0
    np.testing.assert_allclose(out["precip"].values, [2, 3, 4])
    assert out["precip"].attrs["aggregation_period"] == "15 minute"


def test_step_axis(tmp_path, regularize):
    """Overlapping lead windows on step regularize from lead 0."""
    attrs = {"units": "mm day-1", "data_interval": "2 day"}
    ds = xr.Dataset(
        {"precip": (("step",), [1.0, 3.0], attrs)},
        coords={"step": np.array([0, 1]) * DAY},
    )
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["precip"].values, [1, 2, 3])
    assert out["step"].values.tolist() == (np.arange(3) * DAY).astype("m8[ns]").tolist()


def test_cell_missing_a_sample_others_have_is_nan(tmp_path, regularize):
    """A cell with one of two overlapping samples is not averaged over what it has."""
    ds = _samples([(1, 2), (2, 3)], [1, 2])
    ds = ds.reindex(lat=[0.0, 1.0], fill_value=1.0)
    ds["precip"][1, 1] = np.nan
    vals = _run(regularize, tmp_path, ds, "--period", "1 day")["precip"].values
    np.testing.assert_allclose(vals[:, 0], [1, 1.5, 2])
    assert vals[0, 1] == 1 and np.isnan(vals[1, 1]) and np.isnan(vals[2, 1])


def test_period_off_sample_edges_suggests_one(tmp_path, regularize, capsys):
    ds = _samples([(1, 7), (2, 8)], [1, 2])
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, ds, "--period", "7 day")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "1 day" in err and "aggregate-temporal" in err


def test_no_interval_exits_2(tmp_path, regularize):
    ds = xr.Dataset(
        {"precip": (("time",), [1.0, 2.0], {"units": "mm day-1"})},
        coords={"time": [JAN1, JAN1 + DAY]},
    )
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, ds, "--period", "1 day")
    assert exc.value.code == 2


def test_integer_variable_is_averaged_as_float(tmp_path, regularize):
    ds = _samples([(1, 2), (2, 3)], [1, 2])
    ds["count"] = (("time", "lat"), np.array([[1], [2]]), {"units": "1"})
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["count"].values[:, 0], [1, 1.5, 2])


def test_init_axis_exits_2(tmp_path, regularize, capsys):
    """Inits labelled forecast_reference_time are not the dates the samples cover."""
    ds = _samples([(1, 7), (2, 8)], [1, 2])
    ds["time"].attrs["standard_name"] = "forecast_reference_time"
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, ds, "--period", "1 day")
    assert exc.value.code == 2
    assert "step-to-time" in capsys.readouterr().err


def test_single_sample_time_series_defaults_to_time(tmp_path, regularize):
    ds = _samples([(1, 3)], [5])
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["precip"].values[:, 0], [5, 5, 5])


def test_static_variable_passes_through(tmp_path, regularize):
    ds = _samples([(1, 2), (2, 3)], [1, 2])
    ds["mask"] = (("lat",), [1.0])
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    assert out["mask"].dims == ("lat",) and out["mask"].values.tolist() == [1.0]


def test_grid_starts_at_earliest_sample(tmp_path, regularize):
    """Station days running 06-06 UTC stay 06-06 days on a 1-day grid."""
    t0 = np.datetime64("2026-01-01T06:00", "ns")
    attrs = {"units": "mm day-1", "data_interval": "2 day"}
    ds = xr.Dataset({"precip": (("time",), [1.0, 3.0], attrs)}, coords={"time": [t0, t0 + DAY]})
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_array_equal(out["time"].values, t0 + np.arange(3) * DAY)
    np.testing.assert_allclose(out["precip"].values, [1, 2, 3])


def test_dry_cells_stay_exactly_zero(tmp_path, regularize):
    """Averaging adds only the samples covering each cell, so zeros are not rounded away."""
    ds = _samples([(1, 3), (2, 4), (4, 6)], [0.1, 0.2, 0.0])
    vals = _run(regularize, tmp_path, ds, "--period", "1 day")["precip"].values[:, 0]
    assert vals[4] == 0.0 and vals[5] == 0.0


def _two_daily(attrs):
    return xr.Dataset(
        {"precip": (("time",), [1.0, 2.0], {"units": "mm day-1", **attrs})},
        coords={"time": [JAN1, JAN1 + DAY]},
    )


def test_interval_comes_from_variables_on_the_axis(tmp_path, regularize):
    """A static variable's aggregation_period does not set the samples' length."""
    ds = _two_daily({"aggregation_period": "1 day"})
    ds["clim"] = (("lat",), [3.0], {"aggregation_period": "30 day"})
    ds = ds.assign_coords(lat=[0.0])
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["precip"].values, [1, 2])


def test_variables_with_different_intervals_exit_2(tmp_path, regularize):
    ds = _two_daily({"aggregation_period": "7 day"})
    ds["other"] = (("time",), [1.0, 2.0], {"units": "mm day-1", "aggregation_period": "1 day"})
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, ds, "--period", "1 day")
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "attrs",
    [
        {"cell_methods": "time: maximum", "data_interval": "1 day"},
        {"cell_methods": "time: sum", "data_interval": "1 day"},
        {
            "units": "mm",
            "standard_name": "lwe_thickness_of_precipitation_amount",
            "data_interval": "1 day",
        },
    ],
)
def test_extremes_sums_and_amounts_exit_2(tmp_path, regularize, attrs):
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, _two_daily(attrs), "--period", "1 day")
    assert exc.value.code == 2


def test_methods_on_other_dims_are_kept(tmp_path, regularize):
    ds = _two_daily(
        {"cell_methods": "number: mean time: mean (interval: 1 day)", "data_interval": "1 day"}
    )
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    assert out["precip"].attrs["cell_methods"] == "number: mean time: mean"


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_rolling_windows_land_on_their_own_days(tmp_path, regularize, align):
    """aggregate-temporal --window writes each window's bounds, whatever its alignment."""
    aggregate = load_skill("aggregate-temporal", "aggregate").aggregate
    times = JAN1 + np.arange(10) * DAY
    ds = xr.Dataset(
        {
            "precip": (
                ("time",),
                np.arange(10.0),
                {"units": "mm day-1", "standard_name": "lwe_precipitation_rate"},
            )
        },
        coords={"time": times},
    )
    rolled = tmp_path / "rolled.zarr"
    run_skill(
        aggregate,
        "-i",
        str(write_zarr(ds, tmp_path / "in.zarr")),
        "-o",
        str(rolled),
        "--window",
        "3 day",
        "--align",
        align,
    )
    out = _run(regularize, tmp_path, xr.open_zarr(rolled), "--period", "1 day")
    # Day d is covered by the full windows containing it; each is the mean of 3 days.
    window_starts = times + np.timedelta64({"left": 0, "center": -1, "right": -2}[align], "D")
    for day, value in zip(out["time"].values, out["precip"].values, strict=True):
        means = [
            np.mean(
                [v for t, v in zip(times, np.arange(10.0), strict=True) if s <= t < s + 3 * DAY]
            )
            for s in window_starts
            if s <= day < s + 3 * DAY
        ]
        np.testing.assert_allclose(value, np.mean(means))


def test_bounds_stored_nv_first(tmp_path, regularize):
    ds = _samples([(1, 2), (2, 3)], [1, 2])
    ds["time_bounds"] = ds["time_bounds"].transpose("nv", "time")
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out["precip"].values[:, 0], [1, 1.5, 2])


def test_sub_second_edges_and_missing_labels_exit_2(tmp_path, regularize, capsys):
    t0 = np.datetime64("2026-01-01T00:00:00", "ns")
    ds = xr.Dataset(
        {"precip": (("time",), [1.0, 2.0], {"units": "mm hr-1", "data_interval": "1 second"})},
        coords={"time": [t0, t0 + np.timedelta64(500, "ms")]},
    )
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, ds, "--period", "1 second")
    assert exc.value.code == 2 and "whole seconds" in capsys.readouterr().err


def test_per_point_coverage_exits_2(tmp_path, regularize):
    ds = _samples([(1, 2), (2, 3)], [1, 2])
    ds = ds.assign_coords({AGGREGATION_COVERAGE_COORD: (("time", "lat"), [[1.0], [0.5]])})
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, ds, "--period", "1 day")
    assert exc.value.code == 2


def test_scalar_coverage_applies_to_every_sample(tmp_path, regularize):
    """select leaves one coverage for all samples (week 1 of every init)."""
    ds = _samples([(1, 2), (2, 3)], [1, 2]).assign_coords({AGGREGATION_COVERAGE_COORD: 0.5})
    out = _run(regularize, tmp_path, ds, "--period", "1 day")
    np.testing.assert_allclose(out[AGGREGATION_COVERAGE_COORD].values, [0.5, 0.5, 0.5])


@pytest.mark.parametrize("period", ["1 month", "2 months", "1 yr"])
def test_calendar_periods_exit_2(tmp_path, regularize, capsys, period):
    with pytest.raises(SystemExit) as exc:
        _run(regularize, tmp_path, _samples([(1, 2), (2, 3)], [1, 2]), "--period", period)
    assert exc.value.code == 2 and "calendar length" in capsys.readouterr().err

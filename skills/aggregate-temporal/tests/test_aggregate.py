"""Correctness tests for aggregate-temporal."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import (
    load_skill,
    make_forecast,
    make_gridded,
    make_point_obs,
    run_skill,
    write_zarr,
)
from weather_skills_core.provenance import load_history
from weather_skills_core.units import AGGREGATION_COVERAGE_COORD


@pytest.fixture(scope="module")
def aggregate():
    return load_skill("aggregate-temporal", "aggregate").aggregate


def test_aggregate_weekly_mean(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=14, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--method",
        "mean",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    assert float(ds["precip"].values.flat[0]) == pytest.approx(2.0)
    assert ds["precip"].attrs.get("aggregation_period") == "7 day"
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)
    assert load_history(out)[-1]["skill"] == "aggregate-temporal"


def test_aggregate_keeps_incomplete_trailing_week(tmp_path, aggregate, capsys):
    """15 daily samples → 3 weeks; last bin is kept and stamped coverage 1/7."""
    src = write_zarr(make_gridded(n_time=15, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 3
    assert float(ds["aggregation_coverage"].values[-1]) == pytest.approx(1 / 7)
    err = capsys.readouterr().err
    assert "dropped" not in err


def test_aggregate_keeps_incomplete_trailing_month(tmp_path, aggregate):
    # Jan (31) + Feb 1..10 → keep February with coverage 10/28 (2026 is not a leap year).
    src = write_zarr(
        make_gridded(n_time=41, fill=1.0, start="2026-01-01"),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "monthly")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    assert str(ds.time.values[0])[:10] == "2026-01-01"
    assert str(ds.time.values[1])[:10] == "2026-02-01"
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)
    assert float(ds["aggregation_coverage"].values[1]) == pytest.approx(10 / 28)


def test_aggregate_end_time_weekly_two_bins(tmp_path, aggregate):
    """15 days through 2026-08-30 → [Aug 16, Aug 23) and [Aug 23, Aug 30), left-labeled."""
    src = write_zarr(
        make_gridded(n_time=15, fill=2.0, start="2026-08-16"),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-30",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    labels = [str(t)[:10] for t in ds.time.values]
    assert labels == ["2026-08-16", "2026-08-23"]
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)
    assert float(ds["aggregation_coverage"].values[1]) == pytest.approx(1.0)


def test_aggregate_end_time_keeps_incomplete_leading(tmp_path, aggregate, capsys):
    """Series starting mid-bin: leading short week is kept and stamped coverage < 1."""
    # 2026-08-20 .. 2026-08-30 (11 days): full [Aug 23, Aug 30), partial [Aug 16, Aug 23).
    src = write_zarr(
        make_gridded(n_time=11, fill=1.0, start="2026-08-20"),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-30",
    )

    ds = xr.open_zarr(out, consolidated=True)
    labels = [str(t)[:10] for t in ds.time.values]
    assert labels == ["2026-08-16", "2026-08-23"]
    assert float(ds["aggregation_coverage"].values[0]) < 1.0
    assert float(ds["aggregation_coverage"].values[1]) == pytest.approx(1.0)
    assert "dropped" not in capsys.readouterr().err


def _time_bounds_forecast():
    """step-to-time output for GEFS-like leads: 3 h cells for 2 days, then 6 h, with time_bounds."""
    start = np.datetime64("2026-08-24T00", "ns")
    hours = np.concatenate([np.arange(0, 48, 3), np.arange(48, 14 * 24 + 1, 6)])
    edges = start + hours.astype("timedelta64[h]")
    rate = np.where(edges[:-1] < start + np.timedelta64(2, "D"), 1.0, 4.0)
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), rate[:, None, None], {"units": "mm day-1"})},
        coords={
            "time": edges[:-1],
            "time_bounds": (("time", "nv"), np.stack([edges[:-1], edges[1:]], axis=1)),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    ds["time"].attrs.update(standard_name="time", axis="T", bounds="time_bounds")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    return ds


@pytest.mark.parametrize(
    "extra",
    [["--end-time", "2026-09-07"], ["--start-time", "2026-08-24", "--end-time", "2026-09-07"]],
)
def test_end_time_weekly_on_time_bounds(tmp_path, aggregate, extra):
    """Anchored bins on a time_bounds axis weight each cell by its duration."""
    src = write_zarr(_time_bounds_forecast(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly", *extra)
    ds = xr.open_zarr(out, consolidated=True)
    assert [str(t)[:10] for t in ds.time.values] == ["2026-08-24", "2026-08-31"]
    # Week 1: 2 days at 1 mm/day + 5 days at 4 mm/day; week 2: all 4 mm/day.
    np.testing.assert_allclose(ds["precip"].values.ravel(), [22 / 7, 4.0])
    np.testing.assert_allclose(ds["aggregation_coverage"].values, [1.0, 1.0])
    assert ds["precip"].attrs["units"] == "mm day-1"


def test_end_time_weekly_left_edge_matches_forecast_week(tmp_path, aggregate):
    """Obs week [Aug 24, Aug 31) is labeled Aug 24, same as forecast step 0 after step-to-time."""
    src = write_zarr(
        make_gridded(n_time=7, fill=1.0, start="2026-08-24"),
        tmp_path / "obs.zarr",
    )
    out = tmp_path / "obs_weekly.zarr"
    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-31",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert str(ds.time.values[0])[:10] == "2026-08-24"
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)


def test_aggregate_duration_21_day(tmp_path, aggregate):
    """Pint duration --period '21 day' bins 21 daily samples into one complete window."""
    src = write_zarr(make_gridded(n_time=21, fill=3.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "21 day")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert float(ds["precip"].values.flat[0]) == pytest.approx(3.0)
    assert ds["precip"].attrs.get("aggregation_period") == "21 day"


def test_aggregate_21_day_partial_stamps_coverage(tmp_path, aggregate):
    """19 of 21 daily samples → one bin with coverage 19/21, stamped 21 day."""
    src = write_zarr(make_gridded(n_time=19, fill=3.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        aggregate,
        "-i",
        str(src),
        "-o",
        str(out),
        "--period",
        "21 day",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert ds["precip"].attrs.get("aggregation_period") == "21 day"
    # data_interval is the output spacing, not the native 1 day.
    assert ds["precip"].attrs.get("data_interval") == "21 day"
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(19 / 21)


def test_aggregate_requires_period_or_window(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=7), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def _s2s_accum_forecast():
    """S2S-like steps with known cumulative tp (mm)."""
    import numpy as np
    import xarray as xr

    days = [0, 7, 10, 14, 20, 21, 28]
    # Amounts added in each interval after step 0: 7, 6, 12, 24, 10, 35
    increments = [0.0, 7.0, 6.0, 12.0, 24.0, 10.0, 35.0]
    accum = np.cumsum(increments)
    ds = xr.Dataset(
        {
            "tp": (
                ("step", "latitude", "longitude"),
                accum.reshape(-1, 1, 1),
                {"units": "mm", "standard_name": "lwe_thickness_of_precipitation_amount"},
            )
        },
        coords={
            "step": np.array(days, dtype="timedelta64[D]").astype("timedelta64[ns]"),
            "time": np.datetime64("2026-01-01", "ns"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(standard_name="forecast_period")
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    return ds, accum, days


def test_irregular_weekly_totals_match_tp_differences(tmp_path, aggregate):
    from conftest import load_skill

    deaccumulate = load_skill("deaccumulate", "deaccumulate").deaccumulate
    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    ds, accum, days = _s2s_accum_forecast()
    src = write_zarr(ds, tmp_path / "in.zarr")
    rates = tmp_path / "rates.zarr"
    weekly = tmp_path / "weekly.zarr"
    totals = tmp_path / "totals.zarr"

    run_skill(deaccumulate, "-i", str(src), "-o", str(rates))
    run_skill(aggregate, "-i", str(rates), "-o", str(weekly), "--period", "weekly")
    run_skill(convert, "-i", str(weekly), "-o", str(totals))

    rates_ds = xr.open_zarr(rates, consolidated=True)
    assert "step_bounds" in rates_ds.coords or "step_bounds" in rates_ds.variables
    assert rates_ds["tp"].attrs.get("data_interval") is None

    weekly_ds = xr.open_zarr(weekly, consolidated=True)
    weekly_idx = {
        int(np.asarray(s).astype("timedelta64[D]").astype(int)): i
        for i, s in enumerate(weekly_ds.step.values)
    }
    assert 14 in weekly_idx
    assert float(weekly_ds["aggregation_coverage"].values[weekly_idx[14]]) == pytest.approx(1.0)

    out = xr.open_zarr(totals, consolidated=True)
    # convert-to-totals --min-coverage 1.0 may drop incomplete weeks, so index the output.
    out_idx = {
        int(np.asarray(s).astype("timedelta64[D]").astype(int)): i
        for i, s in enumerate(out.step.values)
    }
    assert 14 in out_idx
    # Week starting 14d is [14d, 21d) = tp[21] - tp[14]
    assert float(out["tp"].values[out_idx[14]].flat[0]) == pytest.approx(
        accum[days.index(21)] - accum[days.index(14)]
    )


def test_window_refused_on_cf_bounds(tmp_path, aggregate):
    from conftest import load_skill

    deaccumulate = load_skill("deaccumulate", "deaccumulate").deaccumulate
    ds, _, _ = _s2s_accum_forecast()
    src = write_zarr(ds, tmp_path / "in.zarr")
    rates = tmp_path / "rates.zarr"
    out = tmp_path / "out.zarr"
    run_skill(deaccumulate, "-i", str(src), "-o", str(rates))
    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(rates), "-o", str(out), "--window", "7")
    assert exc.value.code == 2


def test_reaggregate_weekly_to_21_day_expects_three_weeks(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=21, fill=2.0), tmp_path / "daily.zarr")
    weekly = tmp_path / "weekly.zarr"
    out = tmp_path / "21d.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    run_skill(aggregate, "-i", str(weekly), "-o", str(out), "--period", "21 day")
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(1.0)


def test_aggregate_all_incomplete_weeks_kept(tmp_path, aggregate):
    """Three daily samples still form one weekly bin; coverage is 3/7."""
    src = write_zarr(make_gridded(n_time=3, fill=1.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert float(ds["aggregation_coverage"].values[0]) == pytest.approx(3 / 7)


def test_convert_default_drops_incomplete_weeks_kept_by_aggregate(tmp_path, aggregate):
    """aggregate keeps the trailing 1-day week; convert-to-totals default drops it."""
    from conftest import load_skill

    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    src = write_zarr(make_gridded(n_time=15, fill=2.0), tmp_path / "in.zarr")
    weekly = tmp_path / "weekly.zarr"
    totals = tmp_path / "totals.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    run_skill(convert, "-i", str(weekly), "-o", str(totals))

    weekly_ds = xr.open_zarr(weekly, consolidated=True)
    totals_ds = xr.open_zarr(totals, consolidated=True)
    assert weekly_ds.sizes["time"] == 3
    assert totals_ds.sizes["time"] == 2
    assert float(weekly_ds["aggregation_coverage"].values[-1]) == pytest.approx(1 / 7)


def _gefs_like_mixed_step():
    h3 = np.arange(3, 241, 3)
    h6 = np.arange(246, 841, 6)
    hours = np.concatenate([h3, h6])
    steps = hours.astype("timedelta64[h]").astype("timedelta64[ns]")
    ds = xr.Dataset(
        {
            "precipitation_surface": (
                ("step", "latitude", "longitude"),
                np.ones((hours.size, 1, 1)),
                {
                    "units": "mm day-1",
                    "standard_name": "lwe_precipitation_rate",
                    "data_interval": "3 hour",
                },
            )
        },
        coords={
            "step": steps,
            "time": np.datetime64("2026-08-18", "ns"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(standard_name="forecast_period")
    ds["time"].attrs.update(standard_name="time", axis="T")
    return ds


def test_gefs_like_mixed_step_weekly_then_totals(tmp_path, aggregate):
    from conftest import load_skill

    step_to_time = load_skill("step-to-time", "step_to_time").step_to_time
    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    src = write_zarr(_gefs_like_mixed_step(), tmp_path / "gefs.zarr")
    as_time = tmp_path / "gefs_time.zarr"
    weekly = tmp_path / "gefs_weekly.zarr"
    totals = tmp_path / "gefs_weekly_mm.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(as_time))
    run_skill(aggregate, "-i", str(as_time), "-o", str(weekly), "--period", "weekly")
    run_skill(convert, "-i", str(weekly), "-o", str(totals), "--min-coverage", "0.5")

    weekly_ds = xr.open_zarr(weekly, consolidated=True)
    out = xr.open_zarr(totals, consolidated=True)
    assert weekly_ds.sizes["time"] >= out.sizes["time"] >= 1
    assert "aggregation_coverage" in weekly_ds.coords
    assert out["precipitation_surface"].attrs["units"] == "mm"


def test_gefs_unfilled_long_leads_stay_nan(tmp_path, aggregate):
    """Unpublished GEFS leads are NaN; duration-weighted mean must not turn them into 0."""
    ds = _gefs_like_mixed_step()
    ds["precipitation_surface"].attrs.pop("data_interval", None)
    ds["precipitation_surface"] = ds["precipitation_surface"].where(
        ds["step"] <= np.timedelta64(21, "D")
    )
    src = write_zarr(ds, tmp_path / "gefs_partial.zarr")
    weekly = tmp_path / "gefs_partial_weekly.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")

    out = xr.open_zarr(weekly, consolidated=True)
    days = np.asarray(out["step"].values).astype("timedelta64[D]").astype(int)
    vals = np.asarray(out["precipitation_surface"].values).reshape(days.size)
    filled = {int(d): v for d, v in zip(days, vals, strict=True)}
    assert filled[0] == pytest.approx(1.0)
    assert filled[7] == pytest.approx(1.0)
    assert filled[14] == pytest.approx(1.0)
    assert np.isnan(filled[21])
    assert np.isnan(filled[28])
    assert 35 not in filled
    cov = {int(d): c for d, c in zip(days, out["aggregation_coverage"].values, strict=True)}
    assert cov[0] == pytest.approx(1.0)
    assert cov[7] == pytest.approx(1.0)
    assert cov[14] == pytest.approx(1.0)
    assert cov[21] == pytest.approx(0.0)
    assert cov[28] == pytest.approx(0.0)


def test_nan_days_do_not_count_as_coverage(tmp_path, aggregate):
    """A missing day inside an otherwise complete week stamps coverage 6/7."""
    ds = make_gridded(n_time=7, fill=2.0)
    ds["precip"].values[3] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    weekly = xr.open_zarr(out, consolidated=True)
    assert weekly.sizes["time"] == 1
    assert float(weekly["aggregation_coverage"].values[0]) == pytest.approx(6 / 7)
    assert float(weekly["precip"].values.flat[0]) == pytest.approx(2.0)


def test_spatial_nan_hole_does_not_reduce_coverage(tmp_path, aggregate):
    """A persistent spatial hole is not a missing time sample."""
    ds = make_gridded(n_time=7, fill=2.0)
    ds["precip"].values[:, 0, 0] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    weekly = xr.open_zarr(out, consolidated=True)
    assert float(weekly["aggregation_coverage"].values[0]) == pytest.approx(1.0)


def test_weekly_period_on_weekly_step_keeps_values(tmp_path, aggregate):
    """Already-weekly step cubes must not be re-binned (6 weeks must stay 6)."""
    ds = make_forecast(name="tp", n_step=6, fill=1.0)
    steps = np.array([np.timedelta64(7 * i, "D") for i in range(6)])
    ds = ds.assign_coords(step=("step", steps))
    for i, value in enumerate((1.0, 4.0, 9.0, 16.0, 25.0, 36.0)):
        ds["tp"].values[i] = value
    ds["tp"].attrs.update(units="mm day-1", data_interval="7 day")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    weekly = xr.open_zarr(out, consolidated=True)
    assert weekly.sizes["step"] == 6
    np.testing.assert_allclose(weekly["tp"].values[:, 0, 0], [1, 4, 9, 16, 25, 36])
    assert weekly["tp"].attrs.get("aggregation_period") == "7 day"
    assert weekly["tp"].attrs.get("data_interval") == "7 day"


def _tahmo_like_week():
    """3 stations x 7 days; S1 reported only 2 days (like TA00442), S2 none."""
    ds = make_point_obs(n_time=7, n_points=3, fill=4.0, start="2026-09-28")
    ds["precip"].values[1, 2:] = np.nan
    ds["precip"].values[2, :] = np.nan
    return ds


@pytest.mark.parametrize("extra", [[], ["--end-time", "2026-10-05"]])
def test_partial_station_week_is_nan_not_extrapolated(tmp_path, aggregate, extra):
    """A station missing days gets NaN; complete stations keep their weekly rate."""
    src = write_zarr(_tahmo_like_week(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly", *extra)

    weekly = xr.open_zarr(out, consolidated=True)
    vals = weekly["precip"].sel(point_id=["S0", "S1", "S2"]).values.reshape(3)
    assert vals[0] == pytest.approx(4.0)
    assert np.isnan(vals[1])
    assert np.isnan(vals[2])
    assert float(weekly["aggregation_coverage"].values[0]) == pytest.approx(1.0)


def test_partial_station_week_totals_never_scale_missing_days(tmp_path, aggregate):
    """End to end: S1's 2 measured days must never become a 7-day total."""
    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    src = write_zarr(_tahmo_like_week(), tmp_path / "in.zarr")
    weekly = tmp_path / "weekly.zarr"
    totals = tmp_path / "totals.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    run_skill(convert, "-i", str(weekly), "-o", str(totals))

    out = xr.open_zarr(totals, consolidated=True)
    vals = out["precip"].sel(point_id=["S0", "S1"]).values.reshape(2)
    assert vals[0] == pytest.approx(28.0)
    assert np.isnan(vals[1])


def test_partial_grid_cell_is_nan(tmp_path, aggregate):
    """Same rule on grids: a cell missing one day is NaN; a land-mask hole stays NaN."""
    ds = make_gridded(n_time=7, fill=2.0)
    ds["precip"].values[3, 0, 1] = np.nan
    ds["precip"].values[:, 0, 0] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    weekly = xr.open_zarr(out, consolidated=True)["precip"].values[0]
    assert np.isnan(weekly[0, 1])
    assert np.isnan(weekly[0, 0])
    assert weekly[1, 1] == pytest.approx(2.0)
    assert int(np.isnan(weekly).sum()) == 2


def _daily_with_gap(stamp_interval):
    """Two weeks of daily 1 mm/day with no row at all for 2026-01-05."""
    days = np.array([d for d in range(14) if d != 4])
    times = (np.datetime64("2026-01-01", "ns") + days.astype("timedelta64[D]")).astype(
        "datetime64[ns]"
    )
    attrs = {"units": "mm day-1", "standard_name": "lwe_precipitation_rate"}
    if stamp_interval:
        attrs["data_interval"] = "1 day"
    return xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), np.ones((days.size, 1, 1)), attrs)},
        coords={"time": times, "latitude": [1.0], "longitude": [10.0]},
    )


@pytest.mark.parametrize("stamp_interval", [True, False])
def test_missing_row_lowers_coverage_of_its_own_week(tmp_path, aggregate, stamp_interval):
    """A missing day is missing coverage, never a neighbour stretched over the gap."""
    src = write_zarr(_daily_with_gap(stamp_interval), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    ds = xr.open_zarr(out, consolidated=True)
    assert ds[AGGREGATION_COVERAGE_COORD].values.tolist() == pytest.approx([6 / 7, 1.0])


@pytest.mark.parametrize("stamp_interval", [True, False])
def test_window_refused_across_missing_rows(tmp_path, aggregate, stamp_interval):
    src = write_zarr(_daily_with_gap(stamp_interval), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            aggregate,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--window",
            "7",
            "--stride",
            "7",
        )
    assert exc.value.code == 2


def test_incomplete_bins_warn_on_stderr(tmp_path, aggregate, capsys):
    src = write_zarr(make_gridded(n_time=9, fill=2.0), tmp_path / "in.zarr")
    run_skill(aggregate, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--period", "weekly")
    err = capsys.readouterr().err
    assert "1 of 2 weekly bin(s) are incomplete" in err
    assert "2026-01-08" in err


def test_gappy_series_warns_bins_straddle_segments(tmp_path, aggregate, capsys):
    """Two 7-day windows a year apart (as after concat): interior bins straddle."""
    import xarray as xr

    parts = [make_gridded(n_time=7, start=s) for s in ("2024-10-20", "2025-10-20")]
    ds = xr.concat(parts, dim="time")
    ds["precip"].attrs["data_interval"] = "1 day"  # as fetchers stamp daily data
    src = write_zarr(ds, tmp_path / "in.zarr")
    run_skill(aggregate, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--period", "weekly")
    err = capsys.readouterr().err
    assert "Aggregate each segment before concat" in err


def test_monthly_refuses_end_time(tmp_path, aggregate):
    src = write_zarr(make_gridded(n_time=40), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            aggregate,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--period",
            "monthly",
            "--end-time",
            "2026-02-01",
        )
    assert exc.value.code == 2


def _weekly(tmp_path, aggregate, n_days):
    src = write_zarr(make_gridded(n_time=n_days, fill=2.0), tmp_path / "daily.zarr")
    weekly = tmp_path / "weekly.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    return weekly


@pytest.mark.parametrize("period", ["monthly", "dekadal", "10 day", "daily"])
def test_reaggregate_weekly_refuses_bins_that_split_weeks(tmp_path, aggregate, period):
    """A week straddling a month edge would land whole in its label's month."""
    weekly = _weekly(tmp_path, aggregate, 63)
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(weekly), "-o", str(out), "--period", period)
    assert exc.value.code == 2
    assert not out.exists()


def test_reaggregate_weekly_refuses_end_time_off_the_week_grid(tmp_path, aggregate):
    weekly = _weekly(tmp_path, aggregate, 28)  # weeks start 2026-01-01
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(
            aggregate,
            "-i",
            str(weekly),
            "-o",
            str(out),
            "--period",
            "14 day",
            "--end-time",
            "2026-01-30",
        )
    assert exc.value.code == 2
    run_skill(
        aggregate,
        "-i",
        str(weekly),
        "-o",
        str(out),
        "--period",
        "14 day",
        "--end-time",
        "2026-01-29",
    )
    assert xr.open_zarr(out).sizes["time"] == 2


def test_reaggregate_counts_partial_week_at_its_coverage(tmp_path, aggregate):
    """Full week + a 3/7 week in one 14-day bin is 10/14 covered, not complete."""
    weekly = _weekly(tmp_path, aggregate, 10)
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(weekly), "-o", str(out), "--period", "14 day")
    cov = xr.open_zarr(out)[AGGREGATION_COVERAGE_COORD].values
    assert cov.tolist() == pytest.approx([10 / 14])


def test_step_bin_on_last_edge_is_kept(tmp_path, aggregate):
    """Daily leads 0..14 d: lead 14 d starts week 3 and must not be dropped."""
    ds = make_forecast(name="tp", n_step=15, fill=1.0)
    ds = ds.assign_coords(
        step=("step", np.arange(15).astype("timedelta64[D]").astype("timedelta64[ns]"))
    )
    ds["tp"].attrs.update(units="mm day-1", data_interval="1 day")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "weekly")
    weekly = xr.open_zarr(out)
    assert weekly.sizes["step"] == 3
    assert weekly[AGGREGATION_COVERAGE_COORD].values.tolist() == pytest.approx([1, 1, 1 / 7])


@pytest.mark.parametrize("calendar, feb_days", [("noleap", 28), ("360_day", 30)])
def test_monthly_on_model_calendar_uses_its_month_length(tmp_path, aggregate, calendar, feb_days):
    import cftime

    times = xr.date_range(
        "2026-02-01", periods=feb_days, freq="D", calendar=calendar, use_cftime=True
    )
    ds = make_gridded(n_time=feb_days).assign_coords(time=times)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "monthly")
    monthly = xr.open_zarr(out, decode_times=xr.coders.CFDatetimeCoder(use_cftime=True))
    assert isinstance(monthly["time"].values[0], cftime.datetime)
    assert monthly[AGGREGATION_COVERAGE_COORD].values.tolist() == pytest.approx([1.0])


def test_end_time_reports_samples_left_out(tmp_path, aggregate, capsys):
    """Copying an inclusive fetch end date drops that day; say so."""
    src = write_zarr(make_gridded(n_time=14, start="2026-01-01"), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        aggregate, "-i", str(src), "-o", str(out), "--period", "weekly", "--end-time", "2026-01-14"
    )
    assert "1 sample(s) at or after --end-time" in capsys.readouterr().err


def test_monthly_on_model_calendar_with_bounds(tmp_path, aggregate):
    """CMIP6 Amon: mid-month noleap labels with [1st, next 1st) bounds."""
    import cftime

    labels = [cftime.DatetimeNoLeap(2030, m, 15, 12) for m in (1, 2, 3)]
    edges = [cftime.DatetimeNoLeap(2030, m, 1) for m in (1, 2, 3, 4)]
    ds = make_gridded(n_time=3).assign_coords(time=labels)
    for i, value in enumerate((1.0, 2.0, 3.0)):
        ds["precip"].values[i] = value
    ds["time"].attrs["bounds"] = "time_bounds"
    ds["time_bounds"] = (("time", "nv"), np.array([edges[:-1], edges[1:]]).T)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "monthly")
    monthly = xr.open_zarr(out, decode_times=xr.coders.CFDatetimeCoder(use_cftime=True))
    # Each month is its own value, not a blend with its neighbour.
    np.testing.assert_allclose(monthly["precip"].values[:, 0, 0], [1.0, 2.0, 3.0])
    assert monthly[AGGREGATION_COVERAGE_COORD].values.tolist() == pytest.approx([1, 1, 1])


def _gfs_like_steps():
    """GFS-like leads: hourly cells to 12 h, then 3-hourly to 24 h, with step_bounds."""
    ends = np.array([*range(1, 13), 15, 18, 21, 24])
    starts = np.array([0, *ends[:-1]])
    rate = np.array([1.0] * 6 + [2.0] * 6 + [3.0, 5.0, 6.0, 8.0])
    step = ends.astype("timedelta64[h]").astype("timedelta64[ns]")
    bounds = np.stack([starts, ends], axis=1).astype("timedelta64[h]").astype("timedelta64[ns]")
    ds = xr.Dataset(
        {"precip": (("step", "latitude", "longitude"), rate[:, None, None])},
        coords={
            "step": step,
            "step_bounds": (("step", "nv"), bounds),
            "time": np.datetime64("2026-10-16T00", "ns"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    ds["step"].attrs.update(standard_name="forecast_period", bounds="step_bounds")
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    return ds


def test_six_hour_period_on_irregular_step_bounds(tmp_path, aggregate):
    """6 h buckets weight hourly and 3-hourly cells by duration; totals follow the period."""
    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
    src = write_zarr(_gfs_like_steps(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "6 hour")
    ds = xr.open_zarr(out, consolidated=True)
    hours = (ds.step.values / np.timedelta64(1, "h")).astype(int).tolist()
    assert hours == [0, 6, 12, 18]
    # [12, 18) is 3 h at 3 + 3 h at 5; [18, 24) is 3 h at 6 + 3 h at 8.
    np.testing.assert_allclose(ds["precip"].values.ravel(), [1.0, 2.0, 4.0, 7.0])
    np.testing.assert_allclose(ds["aggregation_coverage"].values, [1.0, 1.0, 1.0, 1.0])
    assert ds["precip"].attrs["aggregation_period"] == "6 hour"

    totals = tmp_path / "totals.zarr"
    run_skill(convert, "-i", str(out), "-o", str(totals))
    tot = xr.open_zarr(totals, consolidated=True)
    np.testing.assert_allclose(tot["precip"].values.ravel(), [0.25, 0.5, 1.0, 1.75])


def test_six_hour_period_on_hourly_time(tmp_path, aggregate):
    """Hourly obs → 6 h bins aligned to 00 UTC, complete when all six hours are present."""
    times = np.arange(
        np.datetime64("2026-10-16T00", "ns"),
        np.datetime64("2026-10-17T00", "ns"),
        np.timedelta64(1, "h"),
    )
    rate = (np.arange(24) // 6).astype(float)
    ds = make_gridded(n_time=1).isel(time=[0] * 24).assign_coords(time=times)
    ds["precip"] = (
        ("time", "latitude", "longitude"),
        np.broadcast_to(rate[:, None, None], ds["precip"].shape).copy(),
    )
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(out), "--period", "6 hour")
    res = xr.open_zarr(out, consolidated=True)
    assert [str(t)[11:13] for t in res.time.values] == ["00", "06", "12", "18"]
    np.testing.assert_allclose(res["precip"].values[:, 0, 0], [0.0, 1.0, 2.0, 3.0])
    np.testing.assert_allclose(res["aggregation_coverage"].values, [1.0] * 4)
    assert res["precip"].attrs["aggregation_period"] == "6 hour"


@pytest.mark.parametrize(
    "make, period",
    [(lambda: make_gridded(n_time=4), "6 hour"), (_gfs_like_steps, "2 hour")],
    ids=["daily-time", "3h-step-bounds"],
)
def test_period_shorter_than_input_cells_is_refused(tmp_path, aggregate, make, period):
    src = write_zarr(make(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(aggregate, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--period", period)
    assert exc.value.code == 2


def test_date_stride_off_the_axis_is_a_usage_error(tmp_path, aggregate, capsys):
    """--stride month on weekly data picks dates the axis lacks: exit 2, not KeyError."""
    ds = make_gridded(n_time=30, start="2026-01-05")
    weeks = np.datetime64("2026-01-05", "ns") + np.arange(30) * np.timedelta64(7, "D")
    src = write_zarr(ds.assign_coords(time=weeks), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            aggregate,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--window",
            "13",
            "--align",
            "right",
            "--stride",
            "month",
        )
    assert exc.value.code == 2
    assert "integer --stride" in capsys.readouterr().err

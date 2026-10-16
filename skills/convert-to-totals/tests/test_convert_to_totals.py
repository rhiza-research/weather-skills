"""Correctness tests for convert-to-totals."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
    PRECIP_AMOUNT_LONG_NAME,
    STANDARD,
)


@pytest.fixture(scope="module")
def convert_to_totals():
    return load_skill("convert-to-totals", "convert_to_totals").convert_to_totals


@pytest.fixture(scope="module")
def aggregate():
    return load_skill("aggregate-temporal", "aggregate").aggregate


def test_convert_to_totals_from_attr(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=10.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "1 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_to_totals, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].attrs["long_name"] == PRECIP_AMOUNT_LONG_NAME
    assert result["precip"].values == pytest.approx(10.0)
    assert result["precip"].attrs[AGGREGATION_PERIOD_ATTR] == "1 day"
    assert load_history(out)[-1]["skill"] == "convert-to-totals"


def test_convert_to_totals_sets_precip_long_name(tmp_path, convert_to_totals):
    """Period totals get Total precipitation even when the rate had a product name."""
    ds = make_gridded(n_time=2, fill=10.0)
    ds["precip"].attrs.update(
        {
            AGGREGATION_PERIOD_ATTR: "1 day",
            "long_name": "IMERG daily precipitation",
        }
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_to_totals, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["long_name"] == PRECIP_AMOUNT_LONG_NAME
    assert result["precip"].attrs["standard_name"] == STANDARD["precip_amount"]["standard_name"]


def test_convert_to_totals_rewrites_rate_display_names(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=10.0)
    ds["precip"].attrs.update(
        {
            AGGREGATION_PERIOD_ATTR: "1 day",
            "long_name": "precipitation rate",
            "GRIB_name": "Precipitation rate",
            "comment": "Average precipitation rate since the previous forecast step.",
            "short_name": "prate",
            "step_type": "avg",
        }
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_to_totals, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].attrs["standard_name"] == STANDARD["precip_amount"]["standard_name"]
    assert result["precip"].attrs["long_name"] == PRECIP_AMOUNT_LONG_NAME
    assert result["precip"].attrs["GRIB_name"] == PRECIP_AMOUNT_LONG_NAME
    for key in ("comment", "short_name", "step_type"):
        assert key not in result["precip"].attrs


def test_convert_to_totals_refuses_finer_than_period(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=1.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "21 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_convert_to_totals_requires_stamped_period(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=2, fill=5.0)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_convert_to_totals_singleton_time(tmp_path, convert_to_totals):
    """A single aggregated bin (e.g. one weekly mean) converts without a spacing gate."""
    ds = make_gridded(n_time=1, fill=2.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "7 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_to_totals, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].values == pytest.approx(14.0)


def test_convert_to_totals_after_select_collapsed_time(tmp_path, convert_to_totals):
    """Spatial-only map (time already selected away) still converts from aggregation_period."""
    ds = make_gridded(n_time=1, fill=2.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "7 day"
    ds["precip"].attrs["cell_methods"] = "time: mean"
    ds = ds.isel(time=0, drop=True)
    assert "time" not in ds.dims
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_to_totals, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert list(result["precip"].dims) == ["latitude", "longitude"]
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].attrs["cell_methods"] == "time: sum"
    assert result["precip"].values == pytest.approx(14.0)
    assert result["precip"].attrs[AGGREGATION_PERIOD_ATTR] == "7 day"


def test_convert_min_coverage_default_rejects_partial(tmp_path, convert_to_totals, capsys):
    ds = make_gridded(n_time=1, fill=1.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "21 day"
    ds["precip"].attrs[DATA_INTERVAL_ATTR] = "1 day"
    ds = ds.assign_coords({AGGREGATION_COVERAGE_COORD: ("time", [0.9])})
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2

    run_skill(
        convert_to_totals,
        "-i",
        str(src),
        "-o",
        str(out),
        "--min-coverage",
        "0.6",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm"
    assert result["precip"].values == pytest.approx(21.0)
    # The estimated bin stays identifiable, and the run says so.
    assert result[AGGREGATION_COVERAGE_COORD].values.tolist() == pytest.approx([0.9])
    assert "estimates, not observed totals" in capsys.readouterr().err


def test_convert_refuses_overlapping_21_day(tmp_path, convert_to_totals):
    times = np.array(["2026-01-21", "2026-01-31"], dtype="datetime64[ns]")
    data = np.ones((2, 2, 2))
    ds = xr.Dataset(
        {
            "precip": (
                ("time", "latitude", "longitude"),
                data,
                {
                    "units": "mm day-1",
                    "standard_name": "lwe_precipitation_rate",
                    AGGREGATION_PERIOD_ATTR: "21 day",
                    DATA_INTERVAL_ATTR: "1 day",
                },
            )
        },
        coords={
            "time": times,
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_full_week_of_halfhourly_converts(tmp_path, aggregate, convert_to_totals):
    n = 7 * 48
    times = np.arange(
        np.datetime64("2026-01-01T00:00"),
        np.datetime64("2026-01-01T00:00") + np.timedelta64(n * 30, "m"),
        np.timedelta64(30, "m"),
    )
    ds = xr.Dataset(
        {
            "precip": (
                ("time", "latitude", "longitude"),
                np.full((n, 1, 1), 1.0),
                {
                    "units": "mm day-1",
                    "standard_name": "lwe_precipitation_rate",
                    DATA_INTERVAL_ATTR: "30 minute",
                },
            )
        },
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    src = write_zarr(ds, tmp_path / "half.zarr")
    weekly = tmp_path / "weekly.zarr"
    totals = tmp_path / "totals.zarr"

    run_skill(aggregate, "-i", str(src), "-o", str(weekly), "--period", "weekly")
    run_skill(convert_to_totals, "-i", str(weekly), "-o", str(totals))

    result = xr.open_zarr(totals, consolidated=True)
    assert result.sizes["time"] == 1
    assert result["precip"].attrs["units"] == "mm"
    assert float(result["precip"].values.flat[0]) == pytest.approx(7.0)


def test_convert_to_totals_refuses_precip_totals(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=1, fill=10.0)
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "1 day"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def _daily(variables, n_days):
    times = np.arange(np.datetime64("2026-01-01"), np.datetime64("2026-01-01") + n_days)
    return xr.Dataset(
        {
            name: (("time", "latitude", "longitude"), np.full((n_days, 2, 2), value), attrs)
            for name, (value, attrs) in variables.items()
        },
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )


PRECIP_RATE = {"units": "mm day-1", "standard_name": "lwe_precipitation_rate"}


def _aggregate_then_convert(tmp_path, aggregate, convert_to_totals, ds, period, *args):
    src = write_zarr(ds, tmp_path / "daily.zarr")
    run_skill(aggregate, "-i", str(src), "-o", str(tmp_path / "agg.zarr"), "--period", period)
    out = tmp_path / "totals.zarr"
    run_skill(convert_to_totals, "-i", str(tmp_path / "agg.zarr"), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True)


def test_converts_only_precip_and_passes_other_variables_through(
    tmp_path, aggregate, convert_to_totals
):
    ds = _daily(
        {
            "precip": (2.0, PRECIP_RATE),
            "t2m": (25.0, {"units": "degree_Celsius", "standard_name": "air_temperature"}),
            "surface_pressure": (1000.0, {"units": "hPa"}),
            "ws": (3.0, {"units": "m s-1"}),
        },
        n_days=14,
    )
    result = _aggregate_then_convert(tmp_path, aggregate, convert_to_totals, ds, "weekly")
    assert result["precip"].values == pytest.approx(14.0)
    assert result["precip"].attrs["units"] == "mm"
    for name, value, units in (
        ("t2m", 25.0, "degree_Celsius"),
        ("surface_pressure", 1000.0, "hPa"),
        ("ws", 3.0, "m s-1"),
    ):
        assert result[name].values == pytest.approx(value)
        assert result[name].attrs["units"] == units
        assert result[name].attrs.get("long_name") != PRECIP_AMOUNT_LONG_NAME


def test_refuses_named_non_precip_variable(tmp_path, convert_to_totals):
    ds = make_gridded(n_time=1, name="ws")
    ds["ws"].attrs = {"units": "m s-1", AGGREGATION_PERIOD_ATTR: "7 day"}
    src = write_zarr(ds, tmp_path / "in.zarr")
    for args in ((), ("-v", "ws")):
        with pytest.raises(SystemExit) as exc:
            run_skill(convert_to_totals, "-i", str(src), "-o", str(tmp_path / "out.zarr"), *args)
        assert exc.value.code == 2


def test_monthly_totals_use_each_months_length(tmp_path, aggregate, convert_to_totals):
    ds = _daily({"precip": (2.0, PRECIP_RATE)}, n_days=59)  # Jan + Feb 2026
    result = _aggregate_then_convert(tmp_path, aggregate, convert_to_totals, ds, "monthly")
    totals = result["precip"].isel(latitude=0, longitude=0).values
    assert totals.tolist() == pytest.approx([62.0, 56.0])


def test_refuses_one_overlapping_pair_among_clean_bins(tmp_path, convert_to_totals):
    times = np.array(
        ["2026-01-01", "2026-01-08", "2026-01-15", "2026-01-22", "2026-01-23"],
        dtype="datetime64[ns]",
    )
    ds = xr.Dataset(
        {
            "precip": (
                ("time", "latitude", "longitude"),
                np.ones((5, 2, 2)),
                {**PRECIP_RATE, AGGREGATION_PERIOD_ATTR: "7 day"},
            )
        },
        coords={"time": times, "latitude": [1.0, 2.0], "longitude": [10.0, 11.0]},
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


@pytest.mark.parametrize("method", ["max", "min"])
def test_refuses_weekly_extremes(tmp_path, aggregate, convert_to_totals, method):
    """A weekly max of 10 mm/day is not a 70 mm weekly total."""
    ds = _daily({"precip": (1.0, PRECIP_RATE)}, n_days=7)
    src = write_zarr(ds, tmp_path / "daily.zarr")
    agg = tmp_path / "agg.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(agg), "--period", "weekly", "--method", method)
    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(agg), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


@pytest.fixture(scope="module")
def select():
    return load_skill("select", "select_dim").select


def test_monthly_totals_use_real_month_lengths(tmp_path, aggregate, convert_to_totals):
    """Feb and Sep totals are rate × 28 / 30 days, not pint's 30.44-day month."""
    for start, days in (("2026-02-01", 28), ("2026-09-01", 30)):
        src = write_zarr(
            make_gridded(n_time=days, fill=2.0, start=start), tmp_path / f"{start}.zarr"
        )
        agg = tmp_path / f"{start}_m.zarr"
        out = tmp_path / f"{start}_tot.zarr"
        run_skill(aggregate, "-i", str(src), "-o", str(agg), "--period", "monthly")
        run_skill(convert_to_totals, "-i", str(agg), "-o", str(out))
        result = xr.open_zarr(out, consolidated=True)
        assert result["precip"].values == pytest.approx(2.0 * days)


def test_monthly_series_totals_per_month(tmp_path, aggregate, convert_to_totals):
    """Jan–Mar in one series: each month multiplied by its own length."""
    src = write_zarr(make_gridded(n_time=90, fill=1.0, start="2026-01-01"), tmp_path / "in.zarr")
    agg = tmp_path / "m.zarr"
    out = tmp_path / "tot.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(agg), "--period", "monthly")
    run_skill(convert_to_totals, "-i", str(agg), "-o", str(out))
    result = xr.open_zarr(out, consolidated=True)
    per_month = result["precip"].isel(latitude=0, longitude=0).values
    assert per_month == pytest.approx([31.0, 28.0, 31.0])


def test_monthly_select_then_totals_keeps_month_length(
    tmp_path, aggregate, select, convert_to_totals
):
    """select collapses time: February's real length survives as aggregation_period."""
    src = write_zarr(make_gridded(n_time=59, fill=2.0, start="2026-01-01"), tmp_path / "in.zarr")
    agg = tmp_path / "m.zarr"
    feb = tmp_path / "feb.zarr"
    out = tmp_path / "tot.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(agg), "--period", "monthly")
    run_skill(select, "-i", str(agg), "-o", str(feb), "--dim", "time", "--value", "2026-02-01")
    assert xr.open_zarr(feb)["precip"].attrs[AGGREGATION_PERIOD_ATTR] == "28 day"
    run_skill(convert_to_totals, "-i", str(feb), "-o", str(out))
    assert xr.open_zarr(out)["precip"].values == pytest.approx(56.0)


def test_select_keeps_coverage_so_partial_week_is_refused(
    tmp_path, aggregate, select, convert_to_totals
):
    """A 2-of-7-day week selected out of a series is not turned into a 7-day total."""
    src = write_zarr(make_gridded(n_time=9, fill=2.0), tmp_path / "in.zarr")
    agg = tmp_path / "wk.zarr"
    wk2 = tmp_path / "wk2.zarr"
    out = tmp_path / "tot.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(agg), "--period", "weekly")
    run_skill(select, "-i", str(agg), "-o", str(wk2), "--dim", "time", "--index", "1")
    sel = xr.open_zarr(wk2)
    assert float(sel[AGGREGATION_COVERAGE_COORD]) == pytest.approx(2 / 7)

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_to_totals, "-i", str(wk2), "-o", str(out))
    assert exc.value.code == 2


def test_convert_reports_dropped_intervals(tmp_path, aggregate, convert_to_totals, capsys):
    src = write_zarr(make_gridded(n_time=9, fill=2.0), tmp_path / "in.zarr")
    agg = tmp_path / "wk.zarr"
    out = tmp_path / "tot.zarr"
    run_skill(aggregate, "-i", str(src), "-o", str(agg), "--period", "weekly")
    capsys.readouterr()
    run_skill(convert_to_totals, "-i", str(agg), "-o", str(out))
    err = capsys.readouterr().err
    assert "Dropped 1 of 2 time interval(s)" in err
    assert "2026-01-08" in err
    assert xr.open_zarr(out).sizes["time"] == 1


def test_convert_no_complete_interval_error_suggests_fix(tmp_path, convert_to_totals, capsys):
    ds = make_gridded(n_time=1, fill=1.0)
    ds["precip"].attrs[AGGREGATION_PERIOD_ATTR] = "7 day"
    ds = ds.assign_coords({AGGREGATION_COVERAGE_COORD: ("time", [1 / 7])})
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(convert_to_totals, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    err = capsys.readouterr().err
    assert "--probe-latest" in err and "2026-01-01" in err

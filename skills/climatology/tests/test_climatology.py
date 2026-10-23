"""Correctness tests for climatology."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("climatology", "climatology")


def _record(tmp_path, start="2019-01-01", end="2021-12-31"):
    """Daily record whose value is the year minus 2019 plus 10, at every cell."""
    times = pd.date_range(start, end, freq="D")
    ds = make_gridded(n_time=len(times), lats=(0.0, 0.1), lons=(36.0,), start=start)
    ds["precip"].values[:] = (times.year - 2019 + 10).to_numpy()[:, None, None]
    path = tmp_path / "in.zarr"
    ds.to_zarr(path, mode="w")
    return path


def _run(mod, tmp_path, inp, *args):
    out = tmp_path / "out.zarr"
    run_skill(mod.climatology, "-i", str(inp), "-o", str(out), *args)
    return xr.open_zarr(out)


def test_mean_std_and_count_over_years(tmp_path, mod):
    out = _run(
        mod, tmp_path, _record(tmp_path), "--start-time", "2026-03-01", "--end-time", "2026-03-02"
    )
    assert out["precip_avg"].values == pytest.approx(11.0)  # mean of 10, 11, 12
    assert out["precip_std"].values == pytest.approx(1.0)  # sample std of 10, 11, 12
    assert (out["precip_count"].values == 3).all()
    assert out.attrs["climatology_start_time"] == "2019-01-01"
    assert out.attrs["climatology_end_time"] == "2021-12-31"
    assert out["precip_avg"].attrs["units"] == "mm day-1"


def test_feb29_uses_leap_years_only_and_window_pools_neighbours(tmp_path, mod):
    inp = _record(tmp_path, "2019-01-01", "2021-12-31")  # 2020 is the only leap year
    out = _run(mod, tmp_path, inp, "--start-time", "2024-02-29", "--end-time", "2024-02-29")
    assert (out["precip_count"].values == 1).all()
    assert np.isnan(out["precip_std"].values).all()  # one sample: no spread
    out = _run(
        mod,
        tmp_path,
        inp,
        "--start-time",
        "2024-02-29",
        "--end-time",
        "2024-02-29",
        "--window",
        "3",
    )
    assert (out["precip_count"].values == 7).all()  # Feb 28 and Mar 1 from 3 years, Feb 29 from 1


def test_window_wraps_across_the_year_end(tmp_path, mod):
    out = _run(
        mod,
        tmp_path,
        _record(tmp_path),
        "--start-time",
        "2026-01-01",
        "--end-time",
        "2026-01-01",
        "--window",
        "5",
    )
    assert (out["precip_count"].values == 15).all()  # Dec 30, Dec 31, Jan 1-3 from 3 years


def test_missing_values_are_skipped_not_filled(tmp_path, mod):
    times = pd.date_range("2019-01-01", "2021-12-31", freq="D")
    ds = make_gridded(n_time=len(times), lats=(0.0,), lons=(36.0,), start="2019-01-01")
    ds["precip"].values[:] = 5.0
    ds["precip"].values[times.year == 2020] = np.nan
    ds.to_zarr(tmp_path / "gappy.zarr", mode="w")
    out = _run(
        mod,
        tmp_path,
        tmp_path / "gappy.zarr",
        "--start-time",
        "2026-06-01",
        "--end-time",
        "2026-06-01",
    )
    assert (out["precip_count"].values == 2).all()
    assert out["precip_avg"].values == pytest.approx(5.0)


def test_sub_daily_input_is_refused(tmp_path, mod, capsys):
    ds = make_gridded(n_time=4)
    ds = ds.assign_coords(time=pd.date_range("2020-01-01", periods=4, freq="6h"))
    ds.to_zarr(tmp_path / "hourly.zarr", mode="w")
    with pytest.raises(SystemExit):
        _run(
            mod,
            tmp_path,
            tmp_path / "hourly.zarr",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
        )
    assert "aggregate-temporal" in capsys.readouterr().err


def test_output_opens_as_a_skill_input(tmp_path, mod):
    """The count is a coordinate: as a data variable 'precip_count' reads as precip."""
    from weather_skills_core.decorator import _open_zarr

    _run(mod, tmp_path, _record(tmp_path), "--start-time", "2026-01-01", "--end-time", "2026-01-02")
    out = _open_zarr(tmp_path / "out.zarr")
    assert "precip_count" in out.coords
    assert set(out.data_vars) == {"precip_avg", "precip_std"}


def test_weekly_forecast_inits_keep_member_and_lead_dims(tmp_path, mod, capsys):
    """Sparse init dates are accepted; each member and lead gets its own climatology."""
    inits = pd.date_range("2021-01-06", "2023-12-27", freq="7D")
    steps = np.array([0, 1], dtype="timedelta64[D]").astype("timedelta64[ns]")
    values = np.empty((len(inits), 2, 2, 1, 1))
    values[:] = (inits.year - 2021).to_numpy()[:, None, None, None, None]  # 0, 1, 2 by year
    values[:, :, 1] += 10  # lead 1 is 10 higher
    ds = xr.Dataset(
        {"precip": (("time", "number", "step", "latitude", "longitude"), values)},
        coords={
            "time": inits,
            "number": [0, 1],
            "step": steps,
            "latitude": [0.0],
            "longitude": [36.0],
        },
    )
    ds["precip"].attrs.update(units="mm day-1", aggregation_period="1 day")
    ds.to_zarr(tmp_path / "fc.zarr", mode="w")
    out = _run(
        mod,
        tmp_path,
        tmp_path / "fc.zarr",
        "--start-time",
        "2026-01-01",
        "--end-time",
        "2026-12-31",
        "--window",
        "7",
    )
    assert out["precip_avg"].dims == ("time", "number", "step", "latitude", "longitude")
    lead0, lead1 = (out["precip_avg"].sel(step=s) for s in steps)
    assert float((lead1 - lead0).max()) == pytest.approx(10.0)
    assert float(lead0.mean()) == pytest.approx(1.0, abs=0.1)  # roughly one init a year each
    assert int(out["precip_count"].min()) >= 2
    assert "at least 7 days apart" in capsys.readouterr().err


def test_weekly_means_are_refused(tmp_path, mod, capsys):
    path = _record(tmp_path)
    ds = xr.open_zarr(path).load()
    ds["precip"].attrs["aggregation_period"] = "7 day"
    ds.to_zarr(tmp_path / "weekly.zarr", mode="w")
    with pytest.raises(SystemExit):
        _run(
            mod,
            tmp_path,
            tmp_path / "weekly.zarr",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
        )
    assert "needs daily values" in capsys.readouterr().err


def test_single_year_is_refused(tmp_path, mod, capsys):
    with pytest.raises(SystemExit):
        _run(
            mod,
            tmp_path,
            _record(tmp_path, "2020-01-01", "2020-12-31"),
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
        )
    assert "at least two years" in capsys.readouterr().err


def test_coverage_is_not_a_climatology_variable(tmp_path, mod):
    from conftest import with_coverage

    path = _record(tmp_path)
    ds = with_coverage(xr.open_zarr(path).load(), "precip")
    ds.to_zarr(tmp_path / "cov.zarr", mode="w")
    out = _run(
        mod,
        tmp_path,
        tmp_path / "cov.zarr",
        "--start-time",
        "2026-03-01",
        "--end-time",
        "2026-03-01",
    )
    assert not [v for v in out.data_vars if v.startswith("precip_coverage")]

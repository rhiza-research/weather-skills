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

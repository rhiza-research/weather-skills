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


def _members(tmp_path, attrs):
    """Two-member archive of daily inits, 2019-2021: member 0 is the year minus 2019, member 1 is 10 more."""
    inits = pd.date_range("2019-01-01", "2021-12-31", freq="D")
    values = np.empty((len(inits), 2, 1, 1, 1))
    values[:, 0] = (inits.year - 2019).to_numpy()[:, None, None, None]
    values[:, 1] = values[:, 0] + 10
    ds = xr.Dataset(
        {"precip": (("time", "number", "step", "latitude", "longitude"), values)},
        coords={
            "time": inits,
            "number": [0, 1],
            "step": np.array([7], dtype="timedelta64[D]").astype("timedelta64[ns]"),
            "latitude": [0.0],
            "longitude": [36.0],
        },
    )
    ds["precip"].attrs.update(attrs)
    ds.to_zarr(tmp_path / "members.zarr", mode="w")
    return tmp_path / "members.zarr"


def test_quantiles_are_taken_over_the_pooled_values(tmp_path, mod):
    times = pd.date_range("2019-01-01", "2021-12-31", freq="D")
    ds = make_gridded(n_time=len(times), lats=(0.0,), lons=(36.0,), start="2019-01-01")
    ds["precip"].values[:] = times.day_of_year.to_numpy()[:, None, None] % 3  # 0, 1, 2 in turn
    ds["precip"].values[times.year == 2021] += 100
    ds.to_zarr(tmp_path / "in.zarr", mode="w")
    out = _run(
        mod,
        tmp_path,
        tmp_path / "in.zarr",
        "--start-time",
        "2026-06-10",
        "--end-time",
        "2026-06-10",
        "--window",
        "3",
        "--quantiles",
        "0.05,0.5,0.333",
    )
    pooled = np.concatenate(
        [
            ds["precip"].sel(time=f"{y}-06-{d:02d}").values.ravel()
            for y in (2019, 2020, 2021)
            for d in (9, 10, 11)
        ]
    )
    assert out["precip_q05"].item() == pytest.approx(np.quantile(pooled, 0.05))
    assert out["precip_q50"].item() == pytest.approx(np.quantile(pooled, 0.5))
    assert out["precip_q33p3"].item() == pytest.approx(np.quantile(pooled, 0.333))
    assert out["precip_q33p3"].attrs["quantile"] == pytest.approx(0.333)
    assert out["precip_q50"].attrs["units"] == "mm day-1"


def test_pool_members_treats_members_as_samples(tmp_path, mod):
    out = _run(
        mod,
        tmp_path,
        _members(tmp_path, {"units": "mm day-1", "aggregation_period": "1 day"}),
        "--start-time",
        "2026-06-10",
        "--end-time",
        "2026-06-10",
        "--pool-members",
        "--quantiles",
        "0.5",
    )
    assert "number" not in out["precip_avg"].dims
    assert out["precip_avg"].item() == pytest.approx(6.0)  # mean of 0, 1, 2, 10, 11, 12
    assert out["precip_q50"].item() == pytest.approx(6.0)
    assert out["precip_count"].item() == 6
    assert out.attrs["climatology_pooled_members"] == 1


def test_min_fraction_masks_cells_with_too_few_values(tmp_path, mod):
    times = pd.date_range("2019-01-01", "2021-12-31", freq="D")
    ds = make_gridded(n_time=len(times), lats=(0.0, 0.1), lons=(36.0,), start="2019-01-01")
    ds["precip"].values[:] = 5.0
    ds["precip"].values[times.year != 2020, 1] = np.nan  # second cell: one year of three
    ds.to_zarr(tmp_path / "gappy.zarr", mode="w")
    args = ["--start-time", "2026-06-01", "--end-time", "2026-06-01", "--quantiles", "0.5"]
    out = _run(mod, tmp_path, tmp_path / "gappy.zarr", *args, "--min-fraction", "0.5")
    assert out["precip_avg"].values[0, :, 0] == pytest.approx([5.0, np.nan], nan_ok=True)
    assert np.isnan(out["precip_q50"].values[0, 1, 0])
    assert out["precip_count"].values[0, :, 0].tolist() == [3, 1]  # counts are never masked
    out = _run(mod, tmp_path, tmp_path / "gappy.zarr", *args, "--min-fraction", "0.3")
    assert out["precip_avg"].values[0, :, 0] == pytest.approx([5.0, 5.0])


def test_multi_day_lead_windows_are_accepted_on_forecast_archives(tmp_path, mod):
    """aggregation_period on a step dim is the lead window, not the init spacing."""
    out = _run(
        mod,
        tmp_path,
        _members(tmp_path, {"units": "mm", "aggregation_period": "7 day"}),
        "--start-time",
        "2026-06-10",
        "--end-time",
        "2026-06-10",
    )
    assert out["precip_avg"].sel(number=1).values.ravel() == pytest.approx([11.0])
    assert out["precip_avg"].attrs["aggregation_period"] == "7 day"


def test_pool_members_needs_a_member_dim(tmp_path, mod, capsys):
    with pytest.raises(SystemExit):
        _run(
            mod,
            tmp_path,
            _record(tmp_path),
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--pool-members",
        )
    assert "no ensemble member dim" in capsys.readouterr().err

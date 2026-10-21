"""Correctness tests for hindcast-fetch (remote stores mocked)."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("hindcast-fetch", "fetch")


def _ecmwf_store():
    """WeatherBench2 layout: two 2017 issues and one 2018 issue, 20 hindcast years each."""
    issued = pd.to_datetime(["2017-10-02", "2017-10-05", "2018-10-01"])
    years = np.arange(-20, 0, dtype=np.int32)
    inits = np.array([[t + pd.DateOffset(years=int(y)) for y in years] for t in issued], dtype="datetime64[ns]")
    lats = np.array([-1.5, 0.0, 1.5], dtype=np.float32)
    lons = np.array([0.0, 1.5, 358.5], dtype=np.float32)
    steps = np.array([0, 1, 2], dtype="timedelta64[D]").astype("timedelta64[ns]")
    shape = (len(issued), len(years), len(steps), len(lats), len(lons))
    # Encode the hindcast year in the precip value so selection is checkable: 0.001 m * |year|.
    precip = np.broadcast_to((np.abs(years) * 0.001)[None, :, None, None, None], shape).astype(np.float32)
    return xr.Dataset(
        {
            "total_precipitation_24hr": (("forecast_time", "hindcast_year", "prediction_timedelta", "latitude", "longitude"), precip),
            "2m_temperature": (("forecast_time", "hindcast_year", "prediction_timedelta", "latitude", "longitude"), np.full(shape, 300.0, np.float32)),
        },
        coords={
            "forecast_time": issued.values,
            "hindcast_year": years,
            "prediction_timedelta": steps,
            "latitude": lats,
            "longitude": lons,
            "time": (("forecast_time", "hindcast_year"), inits),
            "valid_time": (("forecast_time", "hindcast_year", "prediction_timedelta"), inits[:, :, None] + steps),
        },
    )


def _gefs_store(variable, inits):
    days = (pd.to_datetime(inits) - pd.Timestamp("1960-01-01")).days.values.astype(np.float32)
    shape = (len(inits), 2, 3, 2, 2)
    ds = xr.Dataset(
        {variable: (("init_time", "member", "prediction_timedelta", "lat", "lon"), np.full(shape, 2.0, np.float32))},
        coords={
            "init_time": days,
            "member": np.array([0.0, 1.0], np.float32),
            "prediction_timedelta": np.array([0, 1, 2]),
            "lat": np.array([0.0, 1.0], np.float32),
            "lon": np.array([10.0, 11.0], np.float32),
        },
    )
    # Mirror _open_gefs's decode step on the fake store.
    decoded = pd.Timestamp("1960-01-01") + pd.to_timedelta(days.astype("float64"), "D")
    return ds.assign_coords(init_time=decoded.values.astype("datetime64[ns]"))


def test_ecmwf_picks_window_inits_from_latest_covering_issue_year(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "ec.zarr"
    monkeypatch.setattr(fetch_mod, "_open_ecmwf", lambda members: _ecmwf_store())

    run_skill(
        fetch_mod.fetch, "--dataset", "ecmwf", "--start-time", "2014-10-01", "--end-time", "2016-10-31",
        "-v", "precip", "-v", "tmp2m", "-o", str(out),
    )

    with xr.open_zarr(out) as ds:
        # Issue year 2018 covers 1998–2017 and is the latest that fits; it has one issue (Oct 1).
        assert ds.attrs["hindcast_issue_year"] == 2018
        assert list(pd.DatetimeIndex(ds["time"].values).strftime("%Y-%m-%d")) == ["2014-10-01", "2015-10-01", "2016-10-01"]
        assert set(pd.DatetimeIndex(ds["issue_time"].values).strftime("%Y-%m-%d")) == {"2018-10-01"}
        assert list(ds["precip"].dims) == ["time", "step", "latitude", "longitude"]
        # Hindcast years -4, -3, -2 -> 4, 3, 2 mm day-1.
        np.testing.assert_allclose(ds["precip"].isel(step=0, latitude=0, longitude=0).values, [4.0, 3.0, 2.0], rtol=1e-5)
        assert ds["precip"].attrs["units"] == "mm day-1"
        assert ds["tmp2m"].attrs["units"] == "degree_Celsius"
        np.testing.assert_allclose(ds["tmp2m"].values, 26.85, atol=1e-3)
        assert float(ds["longitude"].min()) < 0  # 358.5 wrapped to -1.5
    assert load_history(out)[-1]["skill"] == "hindcast-fetch"


def test_ecmwf_explicit_issue_year(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "ec2017.zarr"
    monkeypatch.setattr(fetch_mod, "_open_ecmwf", lambda members: _ecmwf_store())

    run_skill(
        fetch_mod.fetch, "--dataset", "ecmwf", "--start-time", "2010-10-01", "--end-time", "2010-10-31",
        "--issue-year", "2017", "-o", str(out),
    )

    with xr.open_zarr(out) as ds:
        assert list(pd.DatetimeIndex(ds["time"].values).strftime("%Y-%m-%d")) == ["2010-10-02", "2010-10-05"]


def test_ecmwf_window_no_issue_year_covers_exits(fetch_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_mod, "_open_ecmwf", lambda members: _ecmwf_store())
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch, "--dataset", "ecmwf", "--start-time", "1990-01-01", "--end-time", "2016-12-31",
            "-o", str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 2


def test_gefs_keeps_inits_missing_from_one_variable(tmp_path, fetch_mod, monkeypatch, capsys):
    out = tmp_path / "gefs.zarr"
    stores = {
        "precip": _gefs_store("precip", ["2017-10-04", "2017-10-11", "2017-10-18"]),
        "tmp2m": _gefs_store("tmp2m", ["2017-10-11"]),
    }
    monkeypatch.setattr(fetch_mod, "_open_gefs", lambda variable: stores[variable])

    run_skill(
        fetch_mod.fetch, "--dataset", "gefs", "--start-time", "2017-10-01", "--end-time", "2017-10-31",
        "-v", "precip", "-v", "tmp2m", "-o", str(out),
    )

    with xr.open_zarr(out) as ds:
        assert ds.sizes["time"] == 3
        assert list(ds["precip"].dims) == ["time", "number", "step", "latitude", "longitude"]
        assert list(ds["number"].values) == [0, 1]
        assert int(ds["tmp2m"].isel(step=0, number=0, latitude=0, longitude=0).notnull().sum()) == 1
        assert ds["step"].values[1] == np.timedelta64(1, "D")
    assert "GEFS tmp2m has no run for 2 init(s)" in capsys.readouterr().err


def test_gefs_rejects_ecmwf_only_flags(fetch_mod, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch, "--dataset", "gefs", "--start-time", "2017-10-01", "--end-time", "2017-10-31",
            "--members", "-o", str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 2

"""Correctness tests for arco-era5-fetch (mocked GCS open)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("arco-era5-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _arco_ds():
    ds = make_gridded(n_time=2, start="2026-01-01", name="2m_temperature", fill=280.0)
    ds["2m_temperature"].attrs.update(
        units="K",
        long_name="2 metre temperature",
        standard_name="air_temperature",
    )
    return ds


def test_fetch_writes_zarr_with_mocked_open(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_arco", return_value=_arco_ds()):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-v",
            "2m_temperature",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "2m_temperature" in ds
    assert ds["2m_temperature"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(ds["2m_temperature"].values, 280.0 - 273.15, rtol=1e-5)
    assert ds["2m_temperature"].attrs.get("data_interval") == "1 hour"
    assert load_history(out)[-1]["skill"] == "arco-era5-fetch"


def _hourly_arco_ds(n_hours, n_finite):
    """Pre-allocated hourly axis: only the first n_finite hours hold data."""
    times = np.arange(
        np.datetime64("2026-10-06T00"),
        np.datetime64("2026-10-06T00") + np.timedelta64(n_hours, "h"),
    ).astype("datetime64[ns]")
    data = np.full((n_hours, 2, 2), np.nan)
    data[:n_finite] = 280.0
    ds = xr.Dataset(
        {"2m_temperature": (("time", "latitude", "longitude"), data)},
        coords={"time": times, "latitude": [1.0, 2.0], "longitude": [10.0, 11.0]},
    )
    ds["2m_temperature"].attrs.update(units="K", long_name="2 metre temperature")
    return ds


def test_fetch_drops_all_nan_tail(tmp_path, mod, fetch, capsys):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_arco", return_value=_hourly_arco_ds(72, 48)):
        run_skill(
            fetch,
            "--start-time",
            "2026-10-06",
            "--end-time",
            "2026-10-08",
            "-v",
            "2m_temperature",
            "-o",
            str(out),
        )
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 48
    assert np.isfinite(ds["2m_temperature"].values).all()
    assert "ARCO-ERA5 data ends 2026-10-07T23" in capsys.readouterr().err


def test_fetch_all_nan_window_exits_nonzero(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_arco", return_value=_hourly_arco_ds(72, 0)):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-10-06",
                "--end-time",
                "2026-10-08",
                "-v",
                "2m_temperature",
                "-o",
                str(out),
            )
    assert exc.value.code != 0
    assert not out.exists()


def test_probe_latest_returns_last_day_with_data(mod, fetch, capsys):
    ds = _hourly_arco_ds(24 * 5, 48)
    ds.attrs.update(valid_time_stop="2026-06-30", valid_time_stop_era5t="2026-10-09")
    with patch.object(mod, "_open_arco", return_value=ds):
        run_skill(fetch, "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-07"
    assert "ERA5T" in captured.err


def test_precip_relabelled_to_interval_start(tmp_path, mod, fetch):
    # ERA5 labels each hourly accumulation at the hour's END: the 01 UTC value
    # is 00-01 UTC. The output labels it at the start, so a day's 00..23 rows
    # hold raw 01..24 UTC; instantaneous 2m_temperature is not shifted.
    ds = _hourly_arco_ds(48, 48)
    hours = np.arange(48, dtype=float)
    ds["2m_temperature"].values[:] = 270.0 + hours[:, None, None]
    ds["total_precipitation"] = (("time", "latitude", "longitude"), np.zeros((48, 2, 2)))
    ds["total_precipitation"].values[:] = 1e-4 * hours[:, None, None]
    ds["total_precipitation"].attrs.update(units="m", long_name="Total precipitation")
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_arco", return_value=ds):
        run_skill(
            fetch,
            "--start-time",
            "2026-10-06",
            "--end-time",
            "2026-10-06",
            "-v",
            "2m_temperature",
            "-v",
            "total_precipitation",
            "-o",
            str(out),
        )
    got = xr.open_zarr(out, consolidated=True)
    assert got.sizes["time"] == 24
    assert str(got["time"].values[0])[:13] == "2026-10-06T00"
    tp = got["total_precipitation"].isel(latitude=0, longitude=0).values
    np.testing.assert_allclose(tp, 1e-4 * hours[1:25] * 1000 * 24, rtol=1e-5)
    t2m = got["2m_temperature"].isel(latitude=0, longitude=0).values
    np.testing.assert_allclose(t2m, hours[:24] - 3.15, rtol=1e-5, atol=1e-4)


def test_level_units_stay_hpa(tmp_path, mod, fetch):
    ds = xr.Dataset(
        {"temperature": (("time", "level", "latitude", "longitude"), np.full((2, 2, 2, 2), 250.0))},
        coords={
            "time": np.array(["2026-10-06T00", "2026-10-06T01"], dtype="datetime64[ns]"),
            "level": [500, 850],
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )
    ds["temperature"].attrs.update(
        units="K", long_name="Temperature", standard_name="air_temperature"
    )
    ds["level"].attrs["units"] = "Hectopascal(hPa)"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_arco", return_value=ds):
        run_skill(
            fetch,
            "--start-time",
            "2026-10-06",
            "--end-time",
            "2026-10-06",
            "-v",
            "temperature",
            "-o",
            str(out),
        )
    got = xr.open_zarr(out, consolidated=True)
    assert got["level"].attrs["units"] == "hPa"
    assert got["level"].attrs["standard_name"] == "air_pressure"

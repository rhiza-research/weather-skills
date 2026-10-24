"""Correctness tests for windborne-fetch --product analysis (mocked WindBorne API)."""

import numpy as np
import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("windborne-fetch", "fetch")


def _netcdf(tmp_path, name, value, units):
    """A global field with WindBorne's longitude order (0..180, then -180..0)."""
    import xarray as xr

    lon = np.concatenate([np.arange(0, 180, 30.0), np.arange(-180, 0, 30.0)])
    lat = np.arange(90.0, -91, -30.0)
    ds = xr.Dataset(
        {
            name: (
                ("latitude", "longitude"),
                np.full((lat.size, lon.size), value, "float32"),
                {"units": units},
            )
        },
        coords={"latitude": lat, "longitude": lon},
    )
    path = tmp_path / f"src_{name}.nc"
    ds.to_netcdf(path)
    return path.read_bytes()


def _fake_api(monkeypatch, tmp_path):
    import requests

    # Built once per test, outside the request handler.
    files = {
        "temperature_2m": _netcdf(tmp_path, "temperature_2m", 300.0, "K"),
        "total_precipitation_1h": _netcdf(tmp_path, "total_precipitation_1h", 0.5, "mm"),
        "geopotential_500": _netcdf(tmp_path, "geopotential_500", 57000.0, "m2 s-2"),
    }

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            if url.endswith("/available_times"):
                return _Resp(
                    {
                        "available": [
                            "2026-10-19T12:00:00Z",
                            "2026-10-19T06:00:00Z",
                            "2026-10-18T18:00:00Z",
                        ]
                    }
                )
            return _Resp(content=files[params["variable"]])

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")


def test_range_bbox_units(mod, monkeypatch, tmp_path):
    import xarray as xr

    _fake_api(monkeypatch, tmp_path)
    out = tmp_path / "anl.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "analysis",
        "--start-time",
        "2026-10-19",
        "--bbox",
        "30/0/-30/60",
        "-v",
        "t2m",
        "-v",
        "tp",
        "-v",
        "z",
        "--level",
        "500",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    # tp covers the hour before each analysis and is labelled at its start;
    # t2m keeps the analysis times. Each is NaN at the other's times.
    assert list(ds["time"].values) == [
        np.datetime64("2026-10-19T05:00"),
        np.datetime64("2026-10-19T06:00"),
        np.datetime64("2026-10-19T11:00"),
        np.datetime64("2026-10-19T12:00"),
    ]
    t2m = ds["t2m"].isel(latitude=0, longitude=0).values
    tp = ds["tp"].isel(latitude=0, longitude=0).values
    np.testing.assert_allclose(t2m, [np.nan, 26.85, np.nan, 26.85], rtol=1e-5)
    np.testing.assert_allclose(tp, [12.0, np.nan, 12.0, np.nan])
    assert list(ds["longitude"].values) == [0, 30, 60]
    assert list(ds["latitude"].values) == [-30, 0, 30]
    assert ds["tp"].attrs["data_interval"] == "1 hour"
    assert ds["t2m"].attrs["data_interval"] == "6 hour"
    assert ds["z"].dims == ("time", "vertical", "latitude", "longitude")


def test_repeated_variable_fetched_once(mod, monkeypatch, tmp_path):
    import xarray as xr

    _fake_api(monkeypatch, tmp_path)
    out = tmp_path / "anl.zarr"
    run_skill(mod.fetch, "--product", "analysis", "-v", "t2m", "-v", "t2m", "-o", str(out))
    ds = xr.open_zarr(out)
    assert list(ds.data_vars) == ["t2m"]
    assert ds["time"].values.tolist() == [np.datetime64("2026-10-19T12:00", "ns").tolist()]


def test_level_without_pressure_level_field_errors(mod, monkeypatch, tmp_path):
    _fake_api(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            mod.fetch,
            "--product",
            "analysis",
            "-v",
            "t2m",
            "--level",
            "500",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 2


def test_probe_latest(mod, monkeypatch, tmp_path, capsys):
    _fake_api(monkeypatch, tmp_path)
    run_skill(mod.fetch, "--product", "analysis", "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-10-19"


def test_range_outside_served_errors(mod, monkeypatch, tmp_path):
    _fake_api(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        run_skill(
            mod.fetch,
            "--product",
            "analysis",
            "--start-time",
            "2026-09-01",
            "-o",
            str(tmp_path / "x.zarr"),
        )

"""Correctness tests for windborne-fetch --product clim (mocked WindBorne API)."""

import datetime as dt

import numpy as np
import pytest
from conftest import load_skill, run_skill

START = dt.datetime(2026, 7, 15)


@pytest.fixture(scope="module")
def mod():
    return load_skill("windborne-fetch", "fetch")


def test_hourly_normals_with_rate_precip(mod, monkeypatch, tmp_path):
    import requests
    import xarray as xr

    seen = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "normal": "1991-2020",
                "climatologies": [
                    [
                        {
                            "time": f"{START + dt.timedelta(hours=h):%Y-%m-%dT%H}:00:00Z",
                            "temperature_2m": 15.0 + h,
                            "precipitation": 0.5 + h,
                            "pressure_msl": None,
                        }
                        # 00Z on 15 Jul through 00Z on 16 Jul.
                        for h in range(25)
                    ]
                ],
            }

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            seen.update(params)
            return _Resp()

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")
    out = tmp_path / "clim.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "clim",
        "--point",
        "-1.29,36.82",
        "--start-time",
        "2026-07-15",
        "--end-time",
        "2026-07-15",
        "--rolling-days",
        "7",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    assert seen["start_time"] == "2026-07-15T00:00:00Z"
    # 00Z the next day carries the precipitation of the last hour.
    assert seen["end_time"] == "2026-07-16T00:00:00Z"
    assert seen["rolling_average_days"] == 7
    assert "msl" not in ds.data_vars
    # Exactly the requested day, hourly.
    assert ds.sizes["time"] == 24
    assert ds["time"].values[0] == np.datetime64("2026-07-15T00:00")
    assert ds["time"].values[-1] == np.datetime64("2026-07-15T23:00")
    # t2m is instantaneous and keeps its hour.
    np.testing.assert_allclose(ds["t2m"].values[0], 15.0 + np.arange(24))
    # Precip over (t-1h, t] is labelled at t-1h: 00Z holds the 01Z amount.
    np.testing.assert_allclose(ds["tp"].values[0], (1.5 + np.arange(24)) * 24.0)
    assert ds["t2m"].attrs["units"] == "degree_Celsius"


def test_probe_latest_prints_none(mod, monkeypatch, capsys):
    monkeypatch.delenv("WB_CLIENT_ID", raising=False)
    monkeypatch.delenv("WB_API_KEY", raising=False)
    run_skill(mod.fetch, "--product", "clim", "--probe-latest")
    assert capsys.readouterr().out.strip() == "none"

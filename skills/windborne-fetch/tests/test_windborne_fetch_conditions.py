"""Correctness tests for windborne-fetch --product conditions (mocked WindBorne API)."""

import numpy as np
import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("windborne-fetch", "fetch")


def _fake_api(monkeypatch, payload):
    import requests

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return payload

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")


def _period(start, end, text):
    return {"start_time": start, "end_time": end, "conditions": ["Breeze", "Cloudy"], "text": text}


def test_daily_text_per_point(mod, monkeypatch, tmp_path, capsys):
    import xarray as xr

    payload = {
        "forecast_zero": "2026-10-20T00:00:00Z",
        "initialization_time": "2026-10-20T00:00:00Z",
        "forecasts": [
            {
                "timezone": "Africa/Nairobi",
                "daily": [
                    _period("2026-10-20T00:00:00Z", "2026-10-20T21:00:00Z", "Day one."),
                    _period("2026-10-20T21:00:00Z", "2026-10-21T21:00:00Z", "Day two."),
                ],
            },
            {
                "timezone": "Africa/Nairobi",
                "daily": [_period("2026-10-20T00:00:00Z", "2026-10-20T21:00:00Z", "Other.")],
            },
        ],
    }
    _fake_api(monkeypatch, payload)
    out = tmp_path / "cond.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "conditions",
        "--point",
        "-1.29,36.82,nbo",
        "--point",
        "0.5,35.3",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    assert list(ds["text"].sel(station_id="nbo").values) == ["Day one.", "Day two."]
    # A period one point lacks stays empty.
    assert list(ds["text"].sel(station_id="P1").values) == ["Other.", ""]
    assert ds["conditions"].values[0, 0] == "Breeze; Cloudy"
    assert ds["end_time"].values[0, 1] == np.datetime64("2026-10-21T21:00")
    assert "2026-10-20T21:00:00Z: Day two." in capsys.readouterr().err


def test_point_count_mismatch_errors(mod, monkeypatch, tmp_path):
    _fake_api(monkeypatch, {"forecasts": [], "forecast_zero": "", "initialization_time": ""})
    with pytest.raises(SystemExit):
        run_skill(
            mod.fetch, "--product", "conditions", "--point", "-1,36", "-o", str(tmp_path / "x.zarr")
        )


def test_probe_latest(mod, monkeypatch, capsys):
    _fake_api(monkeypatch, {"available": ["2026-10-19T00:00:00Z", "2026-10-20T06:00:00Z"]})
    run_skill(mod.fetch, "--product", "conditions", "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-20"
    assert "2026-10-20 06 UTC" in captured.err

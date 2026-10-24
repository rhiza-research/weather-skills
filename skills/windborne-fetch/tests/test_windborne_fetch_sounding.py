"""Correctness tests for windborne-fetch --product sounding (mocked WindBorne API)."""

import numpy as np
import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("windborne-fetch", "fetch")


def _fake_api(monkeypatch, respond):
    import requests

    calls = []

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], dict(params or {})))
            return _Resp(respond(url, params or {}))

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")
    return calls


def _respond(url, params):
    if url.endswith("/initialization_times"):
        return {"available": ["2026-10-19T00:00:00Z", "2026-10-19T20:00:00Z"]}
    h = params["forecast_hour"]
    return {
        "forecast_zero": "2026-10-19T18:00:00Z",
        "initialization_time": params["initialization_time"],
        "data": [
            {
                "pressure_hpa": p,
                "altitude_m": 1000.0 - p,
                "temperature_c": 20.0 - (1000 - p) / 10 + h,
                "dewpoint_c": 10.0,
                "specific_humidity_kg_kg": 0.008,
                "wind_u_ms": -3.0,
                "wind_v_ms": 1.0,
            }
            for p in (1000, 850, 500)
        ],
    }


def test_profiles_per_point_and_lead(mod, monkeypatch, tmp_path):
    import xarray as xr

    calls = _fake_api(monkeypatch, _respond)
    out = tmp_path / "snd.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "sounding",
        "--point",
        "-1.29,36.82,nbo",
        "--point",
        "0.5,35.3",
        "--lead",
        "6",
        "--lead",
        "0",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    # Latest run, one request per point and lead.
    soundings = [c for c in calls if c[0] == "interpolated_sounding"]
    assert len(soundings) == 4
    assert {c[1]["initialization_time"] for c in soundings} == {"2026-10-19T20:00:00Z"}
    assert ds["t"].dims == ("station_id", "step", "vertical")
    assert list(ds["vertical"].values) == [1000, 850, 500]
    assert list(ds["step"].values.astype("timedelta64[h]").astype(int)) == [0, 6]
    np.testing.assert_allclose(ds["t"].sel(station_id="nbo").values[1], [26.0, 11.0, -24.0])
    assert ds["gh"].attrs["units"] == "m"
    assert ds["time"].values == np.datetime64("2026-10-19T18:00")


def test_lead_must_be_multiple_of_three(mod, monkeypatch, tmp_path):
    _fake_api(monkeypatch, _respond)
    with pytest.raises(SystemExit):
        run_skill(
            mod.fetch,
            "--product",
            "sounding",
            "--point",
            "-1,36",
            "--lead",
            "4",
            "-o",
            str(tmp_path / "x.zarr"),
        )


def test_probe_latest(mod, monkeypatch, tmp_path, capsys):
    calls = _fake_api(monkeypatch, _respond)
    run_skill(mod.fetch, "--product", "sounding", "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-19"
    assert "--date 2026-10-19 --run 20" in captured.err
    assert [c[0] for c in calls] == ["initialization_times"]

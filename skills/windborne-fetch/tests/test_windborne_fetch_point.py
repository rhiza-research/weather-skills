"""Correctness tests for windborne-fetch --product point (mocked WindBorne API)."""

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
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], dict(params or {})))
            if url.endswith("/initialization_times"):
                return _Resp({"available": ["2026-10-19T00:00:00Z", "2026-10-19T20:00:00Z"]})
            return _Resp(respond(url, params or {}))

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")
    return calls


def _series(lat, lon, fields):
    times = ["2026-10-19T18:00:00Z", "2026-10-19T21:00:00Z", "2026-10-20T00:00:00Z"]
    return [
        {"time": t, "latitude": lat, "longitude": lon, **{k: v[i] for k, v in fields.items()}}
        for i, t in enumerate(times)
    ]


def _body(*series):
    return {
        "forecast_zero": "2026-10-19T18:00:00Z",
        "initialization_time": "2026-10-19T20:00:00Z",
        "forecasts": list(series),
    }


def test_default_bundle_two_points(mod, monkeypatch, tmp_path):
    import xarray as xr

    def respond(url, params):
        assert params["coordinates"] == "-1.29,36.82;0.5,35.3"
        return _body(
            _series(
                -1.29, 36.82, {"temperature_2m": [18, 17, 22], "precipitation": [0.1, 0.5, 1.0]}
            ),
            _series(0.5, 35.3, {"temperature_2m": [15, 14, 20], "precipitation": [0.0, 0.0, 0.25]}),
        )

    _fake_api(monkeypatch, respond)
    out = tmp_path / "pt.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "point",
        "--point",
        "-1.29,36.82,nairobi",
        "--point",
        "0.5,35.3",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    assert list(ds["station_id"].values) == ["nairobi", "P1"]
    assert ds["time"].values == np.datetime64("2026-10-19T18:00")
    # tp at lead h covers (h-3, h]: lead 0 (3 h before forecast zero) dropped,
    # the rest labelled at the interval start; nothing covers [6 h, 9 h).
    assert list(ds["step"].values.astype("timedelta64[h]").astype(int)) == [0, 3, 6]
    np.testing.assert_allclose(ds["tp"].sel(station_id="nairobi").values, [4.0, 8.0, np.nan])
    assert ds["tp"].attrs["units"] == "mm day-1"
    # t2m is instantaneous and keeps its lead.
    np.testing.assert_allclose(ds["t2m"].sel(station_id="P1").values, [15, 14, 20])
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
    assert ds.attrs["windborne_ensemble"] == "mean"


def test_level_and_distribution(mod, monkeypatch, tmp_path):
    import xarray as xr

    def respond(url, params):
        assert params == {
            "coordinates": "-1.29,36.82",
            "max_forecast_hour": 360,
            "initialization_time": "2026-10-19T20:00:00Z",
            "variable": "temperature",
            "level": 850,
            "include_distribution": "true",
        }
        recs = _series(-1.29, 36.82, {"temperature_850": [10, 11, 12]})
        for r in recs:
            r["distribution"] = {
                "mean": r["temperature_850"],
                "std": 0.5,
                "p90": r["temperature_850"] + 1,
            }
        return _body(recs)

    _fake_api(monkeypatch, respond)
    out = tmp_path / "pt.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "point",
        "--point",
        "-1.29,36.82",
        "-v",
        "t",
        "--level",
        "850",
        "--distribution",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    assert set(ds.data_vars) == {"t850", "t850_std", "t850_p90"}
    assert ds["t850_std"].attrs["units"] == "K"
    assert ds["t850_p90"].attrs["units"] == "degree_Celsius"
    assert ds.sizes["step"] == 3


def test_tp_distribution_shifts_with_tp(mod, monkeypatch, tmp_path):
    import xarray as xr

    def respond(url, params):
        assert params["max_forecast_hour"] == 48
        recs = _series(-1.29, 36.82, {"total_precipitation_3h": [0.0, 1.0, 2.0]})
        for r in recs:
            r["distribution"] = {
                "mean": r["total_precipitation_3h"],
                "std": 0.5,
                "p90": r["total_precipitation_3h"] * 2,
                "gt_0p25mm": 0.5,
            }
        return _body(recs)

    _fake_api(monkeypatch, respond)
    out = tmp_path / "pt.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "point",
        "--point",
        "-1.29,36.82",
        "-v",
        "tp",
        "--distribution",
        "--max-lead",
        "2D",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    assert set(ds.data_vars) == {"tp", "tp_std", "tp_p90", "tp_gt_0p25mm"}
    assert list(ds["step"].values.astype("timedelta64[h]").astype(int)) == [0, 3]
    np.testing.assert_allclose(ds["tp_p90"].values[0], [16.0, 32.0])
    np.testing.assert_allclose(ds["tp_gt_0p25mm"].values[0], [0.5, 0.5])
    assert ds["tp_gt_0p25mm"].attrs["units"] == "1"


def test_point_units_for_unknown_field_left_unset(mod):
    assert mod._point_units("total_column_water_vapour") == "kg m-2"
    assert mod._point_units("boundary_layer_height") == "m"
    assert mod._point_units("mystery_index") is None


def test_max_lead_zero_with_default_bundle_errors(mod, monkeypatch, tmp_path, capsys):
    _fake_api(monkeypatch, lambda url, params: {})
    with pytest.raises(SystemExit) as exc:
        run_skill(
            mod.fetch,
            "--product",
            "point",
            "--point",
            "-1,36",
            "--max-lead",
            "0",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 2
    assert "no value at lead 0" in capsys.readouterr().err


def test_level_without_pressure_level_field_errors(mod, monkeypatch, tmp_path):
    _fake_api(monkeypatch, lambda url, params: {})
    with pytest.raises(SystemExit) as exc:
        run_skill(
            mod.fetch,
            "--product",
            "point",
            "--point",
            "-1,36",
            "-v",
            "t2m",
            "--level",
            "500",
            "-o",
            str(tmp_path / "x"),
        )
    assert exc.value.code == 2


def test_probe_latest(mod, monkeypatch, tmp_path, capsys):
    calls = _fake_api(monkeypatch, lambda url, params: {})
    run_skill(mod.fetch, "--product", "point", "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-19"
    assert "--date 2026-10-19 --run 20" in captured.err
    assert [c[0] for c in calls] == ["initialization_times"]


def test_pressure_level_needs_level(mod, monkeypatch, tmp_path):
    _fake_api(monkeypatch, lambda url, params: {})
    with pytest.raises(SystemExit):
        run_skill(
            mod.fetch,
            "--product",
            "point",
            "--point",
            "-1,36",
            "-v",
            "t",
            "-o",
            str(tmp_path / "x.zarr"),
        )


def test_missing_credentials_exit(mod, monkeypatch, tmp_path):
    monkeypatch.delenv("WB_CLIENT_ID", raising=False)
    monkeypatch.delenv("WB_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            mod.fetch, "--product", "point", "--point", "-1,36", "-o", str(tmp_path / "x.zarr")
        )
    assert exc.value.code == 2

"""Correctness tests for openaq-fetch (mocked network + missing key)."""

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("openaq-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_missing_openaq_key_exits_2(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    env = {k: v for k, v in os.environ.items() if k != "OPENAQ_API_KEY"}

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-02",
                "--bbox",
                "2/9/0/11",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_probe_401_is_data_error(mod, fetch):
    resp = MagicMock()
    resp.status_code = 401
    resp.raise_for_status.side_effect = requests.HTTPError("401", response=resp)
    session = MagicMock()
    session.get.return_value = resp

    with (
        patch.dict(os.environ, {"OPENAQ_API_KEY": "bad-key"}),
        patch.object(mod.requests, "Session", return_value=session),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(fetch, "--probe-latest")
    assert exc.value.code != 0


def test_fetch_writes_point_obs_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    sensors = [
        {
            "sensor_id": 1,
            "parameter": "pm25",
            "units": "µg/m³",
            "station_id": "loc1",
            "name": "Test",
            "latitude": 1.0,
            "longitude": 10.0,
        }
    ]

    def fake_sensor_daily(_session, desc, _start_iso, _end_iso):
        return [("2026-01-01", 12.5, 24, 24), ("2026-01-02", 13.0, 24, 24)]

    with (
        patch.dict(os.environ, {"OPENAQ_API_KEY": "test-key"}),
        patch.object(mod, "_find_sensors", return_value=sensors),
        patch.object(mod, "_sensor_daily", side_effect=fake_sensor_daily),
        patch.object(mod.requests, "Session", return_value=MagicMock()),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "2/9/0/11",
            "-v",
            "pm25",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "pm25" in ds
    assert ds["pm25"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "openaq-fetch"


# --- HTTP-level fakes: route by URL so the real pagination/parsing code runs ---


def _resp(payload, status=200, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = payload
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(str(status), response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


_LOCATION = {
    "id": 101,
    "name": "Nairobi",
    "coordinates": {"latitude": -1.3, "longitude": 36.8},
    "sensors": [{"id": 7, "parameter": {"name": "pm25", "units": "µg/m³"}}],
}


def _day(local, utc, value, observed=24):
    return {
        "value": value,
        "period": {"datetimeFrom": {"local": local, "utc": utc}},
        "coverage": {"observedCount": observed, "expectedCount": 24},
    }


def _fake_session(days, locations=(_LOCATION,)):
    def get(url, params=None, timeout=None):
        if url.endswith("/locations"):
            return _resp({"results": list(locations)})
        if "/sensors/" in url and url.endswith("/days"):
            return _resp({"results": days})
        raise AssertionError(f"unexpected URL {url}")

    session = MagicMock()
    session.get.side_effect = get
    return session


@pytest.fixture
def fast(mod, monkeypatch):
    monkeypatch.setattr(mod, "_REQUEST_SPACING_S", 0.0)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    monkeypatch.setenv("OPENAQ_API_KEY", "test-key")


def _run(fetch, out, *extra, start="2024-06-01", end="2024-06-02"):
    run_skill(
        fetch,
        "--start-time",
        start,
        "--end-time",
        end,
        "--bbox",
        "0/36/-2/37",
        "-v",
        "pm25",
        *extra,
        "-o",
        str(out),
    )


def test_daily_values_labelled_by_station_local_date(tmp_path, mod, fetch, fast):
    # East of Greenwich the local-midnight start of 2024-06-01 is 2024-05-31 in UTC;
    # labelling by the utc stamp shifted every value one day early.
    days = [
        _day("2024-06-01T00:00:00+03:00", "2024-05-31T21:00:00Z", 10.0),
        _day("2024-06-02T00:00:00+03:00", "2024-06-01T21:00:00Z", 20.0),
    ]
    out = tmp_path / "o.zarr"
    with patch.object(mod.requests, "Session", return_value=_fake_session(days)):
        _run(fetch, out)
    ds = xr.open_zarr(out)
    got = dict(zip(ds["time"].dt.strftime("%Y-%m-%d").values, ds["pm25"].values[:, 0], strict=True))
    assert got == {"2024-06-01": 10.0, "2024-06-02": 20.0}


def test_partial_days_below_min_coverage_left_missing(tmp_path, mod, fetch, fast, capsys):
    days = [
        _day("2024-06-01T00:00:00+03:00", "2024-05-31T21:00:00Z", 10.0, observed=24),
        _day("2024-06-02T00:00:00+03:00", "2024-06-01T21:00:00Z", 99.0, observed=3),
    ]
    out = tmp_path / "o.zarr"
    with patch.object(mod.requests, "Session", return_value=_fake_session(days)):
        _run(fetch, out)
    ds = xr.open_zarr(out)
    times = list(ds["time"].dt.strftime("%Y-%m-%d").values)
    assert "2024-06-02" not in times or bool(ds["pm25"].sel(time="2024-06-02").isnull().all())
    assert float(ds["pm25"].max()) == 10.0
    assert "fewer than 75%" in capsys.readouterr().err


def test_negative_sentinel_daily_mean_left_missing(tmp_path, mod, fetch, fast):
    days = [
        _day("2024-06-01T00:00:00+03:00", "2024-05-31T21:00:00Z", 10.0),
        _day("2024-06-02T00:00:00+03:00", "2024-06-01T21:00:00Z", -999.0),
    ]
    out = tmp_path / "o.zarr"
    with patch.object(mod.requests, "Session", return_value=_fake_session(days)):
        _run(fetch, out)
    assert float(xr.open_zarr(out)["pm25"].min()) == 10.0


@pytest.mark.parametrize(
    "args",
    [
        ["-v", "pm1"],
        ["--workers", "0"],
        ["--bbox", "-2/36/0/37"],
        ["--min-coverage", "1.5"],
    ],
)
def test_bad_arguments_exit_2_before_network(tmp_path, mod, fetch, fast, args):
    session = _fake_session([])
    argv = ["--start-time", "2024-06-01", "--end-time", "2024-06-02", "--bbox", "0/36/-2/37"]
    with (
        patch.object(mod.requests, "Session", return_value=session),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(fetch, *argv, *args, "-o", str(tmp_path / "o.zarr"))
    assert exc.value.code == 2
    session.get.assert_not_called()


def test_locations_429_names_rate_limit(tmp_path, mod, fetch, fast, capsys):
    session = MagicMock()
    session.get.return_value = _resp({}, status=429)
    with (
        patch.object(mod.requests, "Session", return_value=session),
        pytest.raises(SystemExit) as exc,
    ):
        _run(fetch, tmp_path / "o.zarr")
    assert exc.value.code == 1
    assert "rate limit" in capsys.readouterr().err
    assert session.get.call_count == 2  # one backoff retry


def test_probe_latest_uses_supported_endpoint(mod, fetch, fast, capsys):
    # /locations only accepts order_by=id; the old probe sent order_by=datetimeLast (HTTP 422).
    today = datetime.now(UTC).date().isoformat()

    def get(url, params=None, timeout=None):
        if url.endswith("/locations"):
            if (params or {}).get("order_by", "id") != "id":
                return _resp({"detail": "invalid order_by"}, status=422)
            return _resp({"results": [_LOCATION]})
        if url.endswith("/parameters/2/latest"):
            return _resp(
                {
                    "results": [
                        {"datetime": {"utc": "2024-06-01T10:00:00Z"}},
                        {"datetime": {"utc": f"{today}T01:00:00Z"}},
                        {"datetime": {"utc": "2999-01-01T00:00:00Z"}},  # clock skew
                    ]
                }
            )
        raise AssertionError(url)

    session = MagicMock()
    session.get.side_effect = get
    with patch.object(mod.requests, "Session", return_value=session):
        run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == today

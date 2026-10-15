"""Correctness tests for tahmo-fetch (missing creds + mocked API)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history
from weather_skills_core.units import quantify_dataset


@pytest.fixture(scope="module")
def mod():
    return load_skill("tahmo-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _stations():
    return pd.DataFrame(
        {
            "code": ["TA00001", "TA00002", "XX00001"],
            "location_countrycode": ["KE", "KE", "KE"],
            "location_latitude": [1.0, -10.0, 1.1],
            "location_longitude": [36.0, 36.0, 36.0],
            "location_name": ["Nairobi", "Far south", "Skip prefix"],
        }
    )


def _fake_setup(api, stations, var_meta=None):
    if var_meta is None:
        var_meta = {"pr": {"units": "mm", "description": "Precipitation"}}

    def fake_setup(state):
        state["api"] = api
        state["stations"] = stations
        state["var_meta"] = var_meta
        return api, stations, var_meta

    return fake_setup


def test_missing_tahmo_credentials_exits_2(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    env = {
        k: v for k, v in os.environ.items() if k not in ("TAHMO_API_USERNAME", "TAHMO_API_PASSWORD")
    }

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-02",
                "--station",
                "TA00001",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_fetch_requires_station_or_bbox(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )
    assert exc.value.code == 2


def test_list_stations_requires_bbox(fetch):
    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(fetch, "--list-stations")
    assert exc.value.code == 2


def test_list_stations_prints_tsv(capsys, mod, fetch):
    api = MagicMock()
    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        patch.object(mod, "_ensure_setup", side_effect=_fake_setup(api, _stations())),
    ):
        run_skill(fetch, "--list-stations", "--bbox", "2/35/0/37")

    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert lines[0] == "station_id\tname\tlatitude\tlongitude\tcountry"
    assert any(ln.startswith("TA00001\tNairobi\t") for ln in lines)
    assert all("TA00002" not in ln for ln in lines)
    assert all("XX00001" not in ln for ln in lines)
    assert "1 stations" in captured.err


def test_list_stations_empty_bbox(capsys, mod, fetch):
    api = MagicMock()
    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        patch.object(mod, "_ensure_setup", side_effect=_fake_setup(api, _stations())),
    ):
        run_skill(fetch, "--list-stations", "--bbox", "2/0/1/1")

    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert lines == ["station_id\tname\tlatitude\tlongitude\tcountry"]
    assert "0 stations" in captured.err


def test_unknown_station_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    api = MagicMock()
    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        patch.object(mod, "_ensure_setup", side_effect=_fake_setup(api, _stations())),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--station",
            "TA99999",
            "-o",
            str(out),
        )
    assert exc.value.code == 2


def test_fetch_writes_point_obs_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    stations = _stations()

    daily = pd.DataFrame(
        {
            "precip": [1.5, 2.0],
            "temperature": [20.0, 21.0],
            "humidity": [0.6, 0.7],
            "pressure": [87.0, 87.1],
            "station_id": "TA00001",
        },
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )
    daily.index.name = "time"

    var_meta = {
        "pr": {"units": "mm", "description": "Precipitation"},
        "te": {"units": "degrees Celsius", "description": "Surface air temperature"},
        "rh": {"units": "-", "description": "Relative humidity"},
        "ap": {"units": "kPa", "description": "Atmospheric pressure"},
    }
    api = MagicMock()
    api.getVariables.return_value = var_meta

    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        patch.object(mod, "_ensure_setup", side_effect=_fake_setup(api, stations, var_meta)),
        patch.object(mod, "_station_frame", return_value=daily),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--station",
            "TA00001",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["temperature"].attrs["units"] == "degree_Celsius"
    assert ds["humidity"].attrs["units"] == "1"
    assert ds["pressure"].attrs["units"] == "kPa"
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert list(ds["station_id"].values) == ["TA00001"]
    assert ds["country"].values[0] == "KE"
    assert ds["name"].values[0] == "Nairobi"
    assert load_history(out)[-1]["skill"] == "tahmo-fetch"
    quantify_dataset(ds)


def test_fetch_by_bbox_skips_stations_outside(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    fetched = []

    def fake_frame(_api, station_id, _start, _end):
        fetched.append(station_id)
        daily = pd.DataFrame(
            {"precip": [1.0], "station_id": station_id},
            index=pd.to_datetime(["2026-01-01"]),
        )
        daily.index.name = "time"
        return daily

    api = MagicMock()
    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        patch.object(mod, "_ensure_setup", side_effect=_fake_setup(api, _stations())),
        patch.object(mod, "_station_frame", side_effect=fake_frame),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "2/35/0/37",
            "-o",
            str(out),
        )

    assert fetched == ["TA00001"]
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds["station_id"].values) == ["TA00001"]


def test_station_ids_take_precedence_over_bbox(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    fetched = []

    def fake_frame(_api, station_id, _start, _end):
        fetched.append(station_id)
        daily = pd.DataFrame(
            {"precip": [1.0], "station_id": station_id},
            index=pd.to_datetime(["2026-01-01"]),
        )
        daily.index.name = "time"
        return daily

    api = MagicMock()
    with (
        patch.dict(
            os.environ,
            {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"},
        ),
        patch.object(mod, "_ensure_setup", side_effect=_fake_setup(api, _stations())),
        patch.object(mod, "_station_frame", side_effect=fake_frame),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--station",
            "TA00002",
            "--bbox",
            "2/35/0/37",
            "-o",
            str(out),
        )

    assert fetched == ["TA00002"]


def _five_minute_raw(variable, day, value, drop=0):
    """Every 5-minute stamp closing UTC ``day`` (D 00:05 .. D+1 00:00), minus the last ``drop``."""
    stamps = pd.date_range(f"{day} 00:05", periods=288, freq="5min")[: 288 - drop]
    return pd.DataFrame(
        {
            "time": stamps.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "variable": variable,
            "value": value,
            "quality": 1,
        }
    )


def test_daily_values_need_a_complete_record(mod):
    """Complete days aggregate; partial and empty days stay NaN, for sums and means alike."""
    raw = pd.concat(
        [
            _five_minute_raw("pr", "2026-01-01", 0.1),
            _five_minute_raw("te", "2026-01-01", 20.0),
            _five_minute_raw("pr", "2026-01-02", 0.1, drop=1),  # one record short
            _five_minute_raw("te", "2026-01-02", 21.0, drop=12),  # one hour short
            # 2026-01-03: no records at all
            _five_minute_raw("pr", "2026-01-04", 0.2),
            _five_minute_raw("te", "2026-01-04", 22.0, drop=1),
        ]
    )
    with patch.object(mod, "_fetch_raw", return_value=raw):
        daily = mod._station_frame(MagicMock(), "TA00001", "2026-01-01", "2026-01-04")

    assert list(daily.index) == list(pd.date_range("2026-01-01", "2026-01-04", freq="D"))
    assert daily.loc["2026-01-01", "precip"] == pytest.approx(28.8)
    assert daily.loc["2026-01-01", "temperature"] == pytest.approx(20.0)
    assert pd.isna(daily.loc["2026-01-02", "precip"])
    assert pd.isna(daily.loc["2026-01-02", "temperature"])
    assert pd.isna(daily.loc["2026-01-03", "precip"])
    assert pd.isna(daily.loc["2026-01-03", "temperature"])
    assert daily.loc["2026-01-04", "precip"] == pytest.approx(57.6)
    assert pd.isna(daily.loc["2026-01-04", "temperature"])


def test_midnight_stamp_closes_previous_utc_day(mod):
    """The 00:00 stamp ends the previous day; the window runs (start 00:00, end+1 00:00]."""
    day = _five_minute_raw("pr", "2026-01-01", 0.0)
    day.loc[day.index[-1], "value"] = 5.0  # 2026-01-02T00:00Z
    before = pd.DataFrame(  # closes 2025-12-31, outside the window
        {"time": ["2026-01-01T00:00:00Z"], "variable": "pr", "value": 9.0, "quality": 1}
    )
    raw = pd.concat([before, day])
    with patch.object(mod, "_fetch_raw", return_value=raw) as fetch_raw:
        daily = mod._station_frame(MagicMock(), "TA00001", "2026-01-01", "2026-01-01")

    assert fetch_raw.call_args.args[2:] == ("2026-01-01", "2026-01-02")
    assert list(daily.index) == [pd.Timestamp("2026-01-01")]
    assert daily.loc["2026-01-01", "precip"] == pytest.approx(5.0)


def test_fetch_reports_complete_stations(tmp_path, mod, fetch, capsys):
    """Two of three requested days reported: the station is not complete."""
    daily = pd.DataFrame(
        {"precip": [1.5, 2.0], "station_id": "TA00001"},
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )
    daily.index.name = "time"
    var_meta = {"pr": {"units": "mm", "description": "Precipitation"}}
    api = MagicMock()
    with (
        patch.dict(os.environ, {"TAHMO_API_USERNAME": "user", "TAHMO_API_PASSWORD": "pass"}),
        patch.object(mod, "_ensure_setup", side_effect=_fake_setup(api, _stations(), var_meta)),
        patch.object(mod, "_station_frame", return_value=daily),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-03",
            "--station",
            "TA00001",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert "precip: 0 of 1 stations complete (all 3 days" in capsys.readouterr().err

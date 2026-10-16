"""Correctness tests for ghcn-daily-fetch (mocked network)."""

import gzip
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("ghcn-daily-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _stations():
    return pd.DataFrame(
        {"latitude": [1.0], "longitude": [10.0], "name": ["Test Station"]},
        index=pd.Index(["USW00094728"], name="station_id"),
    )


def _station_frame(station_id, _elements, _start_int, _end_int):
    times = pd.to_datetime(["2026-01-01", "2026-01-02"])
    frame = pd.DataFrame({"precip": [1.0, 2.0], "station_id": station_id}, index=times)
    frame.index.name = "time"
    return frame


def test_fetch_writes_point_obs_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with (
        patch.object(mod, "_load_stations", return_value=_stations()),
        patch.object(mod, "_station_frame", side_effect=_station_frame),
    ):
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

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "precip" in ds
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert "station_id" in ds.dims
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "ghcn-daily-fetch"


def _gz_csv(lines):
    return gzip.compress(("\n".join(lines) + "\n").encode())


def test_station_frame_drops_flagged_and_presumed_zero_precip(mod):
    # Columns: ID,DATE,ELEMENT,VALUE,M_FLAG,Q_FLAG,S_FLAG,OBS_TIME (as in by_station csv.gz).
    content = _gz_csv(
        [
            "USC00300023,20260101,PRCP,0,P,,0,0700",  # missing presumed zero -> NaN
            "USC00300023,20260102,PRCP,0,T,,0,0700",  # trace -> a real 0 mm
            "USC00300023,20260103,PRCP,25,,,0,0700",  # 2.5 mm
            "USC00300023,20260104,PRCP,300,,O,0,0700",  # failed QC -> NaN
            "USC00300023,20260105,PRCP,0,I,,0,0700",  # no precip report -> NaN
            "USC00300023,20260101,TMAX,251,,,0,0700",
            "USC00300023,20260104,TMAX,-9999,,,0,0700",
        ]
    )
    with patch.object(mod, "_fetch_station_csv", return_value=content):
        daily = mod._station_frame(
            "USC00300023", {"PRCP": "precip", "TMAX": "tmax"}, 20260101, 20260105
        )
    precip = daily["precip"].dropna()
    assert list(precip.index.strftime("%Y-%m-%d")) == ["2026-01-02", "2026-01-03"]
    assert list(precip.values) == [0.0, 2.5]
    assert daily["tmax"].dropna().to_dict() == {pd.Timestamp("2026-01-01"): 25.1}


def test_unreported_days_are_nan_rows_up_to_last_report(tmp_path, mod, fetch):
    def station_frame(station_id, _elements, _start_int, _end_int):
        times = pd.to_datetime(["2026-01-02", "2026-01-04"])
        frame = pd.DataFrame({"precip": [0.0, 2.0], "station_id": station_id}, index=times)
        frame.index.name = "time"
        return frame

    out = tmp_path / "out.zarr"
    with (
        patch.object(mod, "_load_stations", return_value=_stations()),
        patch.object(mod, "_station_frame", side_effect=station_frame),
    ):
        run_skill(fetch, "--start-time", "2026-01-01", "--end-time", "2026-01-06", "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    days = pd.DatetimeIndex(ds["time"].values).strftime("%Y-%m-%d")
    assert list(days) == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    precip = ds["precip"].isel(station_id=0).values
    assert np.isnan(precip[[0, 2]]).all()
    assert list(precip[[1, 3]]) == [0.0, 2.0]


def test_probe_latest_reports_newest_observation_not_upload_date(capsys, mod, fetch):
    year = datetime.now(UTC).year

    def read_csv(url, **_kwargs):
        if str(year) in url:
            raise HTTPError(url, 404, "Not Found", None, None)
        assert str(year - 1) in url
        prev = (year - 1) * 10000
        return iter(
            [pd.DataFrame({1: [prev + 1009, prev + 1012]}), pd.DataFrame({1: [prev + 1011]})]
        )

    with patch.object(mod.pd, "read_csv", side_effect=read_csv):
        run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == f"{year - 1}-10-12"


def test_probe_latest_rejects_ident(capsys, fetch):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--probe-latest", "final")
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "takes no argument" in captured.err


def test_station_csvs_come_from_ncei_not_stale_s3_mirror(mod):
    url = mod._STATION_CSV_URL.format(station_id="USW00094728")
    assert url == "https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_station/USW00094728.csv.gz"


def test_fetch_summarizes_stations_and_values(tmp_path, mod, fetch, capsys):
    """A sparse pull (one station, few values) must say so on stderr."""
    with (
        patch.object(mod, "_load_stations", return_value=_stations()),
        patch.object(mod, "_station_frame", side_effect=_station_frame),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-v",
            "precip",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    err = capsys.readouterr().err
    assert "Kept 1 of 1 candidate stations; finite station-days: precip 2 of 2" in err

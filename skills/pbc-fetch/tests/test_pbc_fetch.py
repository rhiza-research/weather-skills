"""Correctness tests for pbc-fetch (network / remote Zarr mocked)."""

import json
from datetime import date
from pathlib import Path

import gcsfs
import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("pbc-fetch", "fetch")


def _pbc_grid(fill=0.2):
    quintile = np.array([0.2, 0.4, 0.6, 0.8, 1.0])
    lats = np.array([2.0, 1.0, 0.0], dtype=np.float32)
    lons = np.array([10.0, 11.5, 13.0, 14.5], dtype=np.float32)
    data = np.full((5, 3, 4), fill, dtype=np.float32)
    return xr.Dataset(
        {"forecast": (("quintile", "latitude", "longitude"), data)},
        coords={"quintile": quintile, "latitude": lats, "longitude": lons},
    )


def test_folder_for_init_week3_and_week4(fetch_mod):
    assert fetch_mod._folder_for_init("era5-p_pr_19", date(2026, 8, 31)) == "20260918"
    assert fetch_mod._folder_for_init("era5-p_pr_26", date(2026, 8, 24)) == "20260918"


def test_init_for_folder_inverts_offset(fetch_mod):
    assert fetch_mod._init_for_folder("era5-p_pr_19", "20260918") == date(2026, 8, 31)
    assert fetch_mod._init_for_folder("era5-p_pr_26", "20260918") == date(2026, 8, 24)


def test_fetch_writes_zarr_and_stamps_history(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "pbc.zarr"
    remote = _pbc_grid()

    monkeypatch.setattr(fetch_mod, "_list_folder_dates", lambda dataset: ["20260911", "20260918"])
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: "20260918" in key)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key, dask=False: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--dataset", "era5-p_pr_19", "-o", str(out))

    assert Path(out).exists()
    with xr.open_zarr(out, consolidated=True) as ds:
        assert "pr" in ds.data_vars
        assert "forecast" not in ds.data_vars
        assert ds["pr"].attrs["units"] == "1"
        # "probability" is not a CF standard name.
        assert "standard_name" not in ds["pr"].attrs
        assert ds["step"].attrs.get("standard_name") == "forecast_period"
        assert ds["pr"].attrs.get("data_interval") == "7 day"
        assert "aggregation_period" not in ds["pr"].attrs
        assert "time" in ds.coords and "time" not in ds.dims
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [18]
        assert np.datetime_as_string(ds["time"].values, unit="D") == "2026-08-31"
        assert list(ds["pr"].dims) == ["quintile", "step", "latitude", "longitude"]
        assert "sheerwater-pbc:era5-p_pr_19" in ds.attrs.get("weather_skills_source", "")
    history = load_history(out)
    assert history[-1]["skill"] == "pbc-fetch"


def test_fetch_honors_init_date_variable_and_bbox(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "pbc_bbox.zarr"
    remote = _pbc_grid()
    seen = {}

    def exists(key):
        seen["key"] = key
        return True

    def fake_open(key, dask=False):
        seen["open"] = key
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_store_exists", exists)
    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "pr_26",
        "--date",
        "2026-08-24",
        "-v",
        "forecast",
        "--bbox",
        "3/9/0/12",
        "-o",
        str(out),
    )

    assert seen["key"] == "pbc-data/era5-p_pr_26/20260918/era5-p_pr_26-20260918.zarr"
    with xr.open_zarr(out, consolidated=True) as ds:
        assert list(ds.data_vars) == ["pr"]
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [25]
        assert float(ds.latitude.min()) >= 0
        assert float(ds.longitude.max()) <= 12


def test_missing_init_is_an_error_not_an_older_folder(tmp_path, fetch_mod, monkeypatch, capsys):
    # Folders are near-daily, so a folder named like the requested init exists
    # (20260918) even though init 2026-09-18 (folder 20261006) is not published.
    # It must not be read as init 2026-08-31.
    opened = []
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: "20260918" in key)
    monkeypatch.setattr(
        fetch_mod, "_open_remote", lambda key, dask=False: opened.append(key) or _pbc_grid()
    )

    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--dataset",
            "era5-p_pr_19",
            "--date",
            "2026-09-18",
            "-o",
            str(tmp_path / "x.zarr"),
        )

    assert exc.value.code != 0
    assert opened == []
    assert "20261006" in capsys.readouterr().err


def test_probe_latest(capsys, fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_list_folder_dates", lambda dataset: ["20260911", "20260918"])
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: "20260918" in key)
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-08-31"


def test_probe_latest_dataset_alias(capsys, fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_list_folder_dates", lambda dataset: ["20260918"])
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)
    run_skill(fetch_mod.fetch, "--probe-latest", "pr_26")
    assert capsys.readouterr().out.strip() == "2026-08-24"


def test_missing_date(fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: False)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--dataset",
            "era5-p_pr_19",
            "--date",
            "2020-01-01",
            "-o",
            "/tmp/x.zarr",
        )
    assert exc.value.code != 0


def test_unknown_variable(tmp_path, fetch_mod, monkeypatch, capsys):
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key, dask=False: _pbc_grid())
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2026-08-31",
            "-v",
            "tas",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code != 0
    assert "tas" in capsys.readouterr().err


def test_gcs_uses_credentials_json_when_set(fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_FS", None)
    info = {"type": "service_account", "project_id": "sheerwater", "client_email": "x@y"}
    monkeypatch.setenv("GCLOUD_SERVICE_CREDENTIALS", json.dumps(info))
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    assert fetch_mod._gcs() == "fake-fs"
    assert seen == {"token": info}


def test_gcs_rejects_invalid_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_FS", None)
    monkeypatch.setenv("GCLOUD_SERVICE_CREDENTIALS", "{not valid json")
    with pytest.raises(fetch_mod.DataError, match="not valid JSON"):
        fetch_mod._gcs()


def test_gcs_falls_back_without_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_FS", None)
    monkeypatch.delenv("GCLOUD_SERVICE_CREDENTIALS", raising=False)
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    assert fetch_mod._gcs() == "fake-fs"
    assert seen == {}


_WINDOW_INITS = [date(2025, 1, 30), date(2025, 1, 31), date(2025, 2, 1), date(2026, 1, 15)]


def _mock_windows(monkeypatch, fetch_mod, *, missing=()):
    """Week-3 folders for _WINDOW_INITS; each store's probability is the init's day / 100."""
    folders = {fetch_mod._folder_for_init("era5-p_pr_19", d): d for d in _WINDOW_INITS}
    monkeypatch.setattr(fetch_mod, "_list_folder_dates", lambda dataset: sorted(folders))
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: not any(f in key for f in missing))

    def fake_open(key, dask=False):
        folder = key.split("/")[-2]
        return _pbc_grid(fill=folders[folder].day / 100)

    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)
    return folders


def test_init_window_keeps_inits_on_a_time_axis(tmp_path, fetch_mod, monkeypatch):
    _mock_windows(monkeypatch, fetch_mod)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch, "--start-time", "2025-01-31", "--end-time", "2025-02-01", "-o", str(out)
    )
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["pr"].dims == ("time", "step", "quintile", "latitude", "longitude")
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
        assert days == ["2025-01-31", "2025-02-01"]
        np.testing.assert_allclose(ds["pr"].values[:, 0, 0, 0, 0], [0.31, 0.01], rtol=1e-6)
        assert ds.attrs["pbc_folder"] == "20250218..20250219"
    assert load_history(out)[-1]["args"]["start_time"] == ["2025-01-31"]


def test_repeated_init_windows_take_disjoint_inits(tmp_path, fetch_mod, monkeypatch):
    _mock_windows(monkeypatch, fetch_mod)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch,
        "--start-time",
        "2025-01-01",
        "--end-time",
        "2025-01-31",
        "--start-time",
        "2026-01-01",
        "--end-time",
        "2026-01-31",
        "-o",
        str(out),
    )
    with xr.open_zarr(out, consolidated=True) as ds:
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2025-01-30", "2025-01-31", "2026-01-15"]


def test_init_window_warns_on_missing_store(tmp_path, fetch_mod, monkeypatch, capsys):
    _mock_windows(monkeypatch, fetch_mod, missing={"20250218"})
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch, "--start-time", "2025-01-30", "--end-time", "2025-02-01", "-o", str(out)
    )
    assert "init 2025-01-31 has no 'era5-p_pr_19' Zarr" in capsys.readouterr().err
    with xr.open_zarr(out, consolidated=True) as ds:
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2025-01-30", "2025-02-01"]


def test_date_and_init_window_together_exit_2(tmp_path, fetch_mod, monkeypatch):
    _mock_windows(monkeypatch, fetch_mod)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2025-01-31",
            "--start-time",
            "2025-01-31",
            "--end-time",
            "2025-02-01",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 2


def test_init_window_with_no_init_exits_1(tmp_path, fetch_mod, monkeypatch):
    _mock_windows(monkeypatch, fetch_mod)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--start-time",
            "2024-06-01",
            "--end-time",
            "2024-06-30",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 1


def _need_dask(fetch):
    pytest.importorskip("dask")
    if "--dask" not in fetch.parser._option_string_actions:
        pytest.skip("installed weather-skills-core has no --dask")


def test_dask_range_of_inits_streams_each_store(tmp_path, fetch_mod, monkeypatch):
    _need_dask(fetch_mod.fetch)
    opened = []
    monkeypatch.setattr(fetch_mod, "_list_folder_dates", lambda dataset: ["20260911", "20260918"])
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda key: True)

    def fake_open(key, dask=False):
        opened.append(dask)
        return _pbc_grid().chunk()

    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)
    out = tmp_path / "pbc.zarr"
    run_skill(
        fetch_mod.fetch,
        "--dask",
        "--dataset",
        "era5-p_pr_19",
        "--start-time",
        "2026-08-01",
        "--end-time",
        "2026-09-30",
        "-o",
        str(out),
    )
    assert len(opened) == 2 and all(d is not None for d in opened)
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.sizes["time"] == 2
        np.testing.assert_allclose(ds["pr"].values, 0.2)

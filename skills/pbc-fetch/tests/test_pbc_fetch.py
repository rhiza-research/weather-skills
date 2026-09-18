"""Correctness tests for pbc-fetch (network / remote Zarr mocked)."""

from datetime import date
from pathlib import Path

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
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--dataset", "era5-p_pr_19", "-o", str(out))

    assert Path(out).exists()
    with xr.open_zarr(out, consolidated=True) as ds:
        assert "pr" in ds.data_vars
        assert "forecast" not in ds.data_vars
        assert ds["pr"].attrs["units"] == "1"
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

    def fake_open(key):
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


def test_fetch_accepts_folder_date_as_fallback(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "pbc_folder.zarr"
    seen = {}

    def exists(key):
        # Init 2026-09-18 would look for folder 20261006 (week 3) — missing.
        return "20260918" in key and "20261006" not in key

    def fake_open(key):
        seen["open"] = key
        return _pbc_grid()

    monkeypatch.setattr(fetch_mod, "_store_exists", exists)
    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "era5-p_pr_19",
        "--date",
        "2026-09-18",
        "-o",
        str(out),
    )

    assert seen["open"] == "pbc-data/era5-p_pr_19/20260918/era5-p_pr_19-20260918.zarr"
    with xr.open_zarr(out, consolidated=True) as ds:
        assert np.datetime_as_string(ds["time"].values, unit="D") == "2026-08-31"


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
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda key: _pbc_grid())
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

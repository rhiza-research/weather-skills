"""Correctness tests for neuralgcm-fetch (network / remote Zarr mocked)."""

import json
from pathlib import Path

import gcsfs
import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("neuralgcm-fetch", "fetch")


def _native_precip(*, fill=0.001, n_step=4, members=2):
    """Source-like cube: metres / 6h, timedelta starting at +6h, lon-before-lat."""
    steps = np.array([np.timedelta64(6 * (i + 1), "h") for i in range(n_step)])
    init = np.array(["2026-09-11T00:00:00"], dtype="datetime64[ns]")
    lats = np.array([0.0, 2.0])
    lons = np.array([10.0, 11.0])
    data = np.full((members, 1, n_step, len(lons), len(lats)), fill, dtype=np.float32)
    return xr.Dataset(
        {
            "total_precipitation_6hr": (
                ("realization", "init_time", "timedelta", "longitude", "latitude"),
                data,
            )
        },
        coords={
            "realization": list(range(members)),
            "init_time": init,
            "timedelta": steps,
            "longitude": lons,
            "latitude": lats,
        },
    )


def _native_surface(*, fill=280.0, n_step=3, members=2):
    steps = np.array([np.timedelta64(6 * (i + 1), "h") for i in range(n_step)])
    init = np.array(["2026-09-11T00:00:00"], dtype="datetime64[ns]")
    lats = np.array([0.0, 2.0])
    lons = np.array([10.0, 11.0])
    data = np.full((members, 1, n_step, len(lons), len(lats)), fill, dtype=np.float32)
    ds = xr.Dataset(
        {
            "2m_temperature": (
                ("realization", "init_time", "timedelta", "longitude", "latitude"),
                data,
            ),
            "2m_dewpoint_temperature": (
                ("realization", "init_time", "timedelta", "longitude", "latitude"),
                data - 5.0,
            ),
        },
        coords={
            "realization": list(range(members)),
            "init_time": init,
            "timedelta": steps,
            "longitude": lons,
            "latitude": lats,
        },
    )
    return ds


def test_canonical_dataset_aliases(fetch_mod):
    assert fetch_mod._canonical_dataset("precip") == "imerg:precip"
    assert fetch_mod._canonical_dataset("imerg:precip") == "imerg:precip"
    assert fetch_mod._canonical_dataset("surface") == "era5:surface"


def test_stamp_to_date(fetch_mod):
    assert fetch_mod._stamp_to_date("20260911T00") == "2026-09-11"
    assert fetch_mod._stamp_to_date("20260911T12") == "2026-09-11"


def test_fetch_writes_zarr_and_stamps_history(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "neuralgcm.zarr"
    remote = _native_precip()
    seen = {}

    monkeypatch.setattr(
        fetch_mod,
        "_list_init_stamps",
        lambda: ["20260910T00", "20260911T00", "20260911T12"],
    )
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: stamp == "20260911T12")

    def fake_open(stamp, dataset):
        seen["stamp"] = stamp
        seen["dataset"] = dataset
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)

    run_skill(fetch_mod.fetch, "-o", str(out))

    assert seen["stamp"] == "20260911T12"
    assert Path(out).exists()
    with xr.open_zarr(out, consolidated=True) as ds:
        assert "tp" in ds.data_vars
        assert "total_precipitation_6hr" not in ds.data_vars
        assert {"number", "step", "latitude", "longitude"} <= set(ds.sizes)
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds["tp"].attrs.get("data_interval")
        hours = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
        assert list(hours) == [0, 6, 12, 18]
        # 0.001 m / 6h = 4 mm day-1
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [4.0, 4.0, 4.0, 4.0], rtol=1e-5)
        assert "neuralgcm:imerg:precip:" in ds.attrs.get("weather_skills_source", "")
        assert "aggregation_period" not in ds["tp"].attrs
    history = load_history(out)
    assert history[-1]["skill"] == "neuralgcm-fetch"


def test_fetch_does_not_deaccumulate_period_totals(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "neuralgcm_period.zarr"
    remote = _native_precip(fill=0.0)
    for i, val in enumerate((0.002, 0.001, 0.0, 0.0005)):
        remote["total_precipitation_6hr"].values[:, 0, i, :, :] = val

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--date", "2026-09-11", "-o", str(out))

    with xr.open_zarr(out, consolidated=True) as ds:
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [8.0, 4.0, 0.0, 2.0], rtol=1e-5)


def test_fetch_honors_date_cycle_variable_and_bbox(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "neuralgcm_bbox.zarr"
    remote = _native_surface()
    seen = {}

    def fake_exists(stamp, dataset):
        seen["key"] = (stamp, dataset)
        return True

    monkeypatch.setattr(fetch_mod, "_store_exists", fake_exists)

    def fake_open(stamp, dataset):
        seen["open"] = (stamp, dataset)
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "era5:surface",
        "--date",
        "2026-09-11",
        "--cycle",
        "12",
        "-v",
        "t2m",
        "--bbox",
        "3/9/0/12",
        "-o",
        str(out),
    )

    assert seen["key"] == ("20260911T12", "era5:surface")
    assert seen["open"] == ("20260911T12", "era5:surface")
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.data_vars) == ["t2m"]
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(ds["t2m"].values.mean(), 6.85, atol=0.01)


def test_probe_latest(capsys, fetch_mod, monkeypatch):
    monkeypatch.setattr(
        fetch_mod,
        "_list_init_stamps",
        lambda: ["20260910T00", "20260911T00", "20260911T12"],
    )
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: stamp.endswith("T12"))
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-09-11"


def test_unknown_variable_exits_2(tmp_path, fetch_mod, monkeypatch):
    remote = _native_precip()
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2026-09-11",
            "-v",
            "t2m",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_missing_date_exits_1(tmp_path, fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: False)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2020-01-01",
            "-o",
            str(tmp_path / "unused.zarr"),
        )
    assert exc.value.code == 1


def test_filesystem_uses_credentials_json_when_set(fetch_mod, monkeypatch):
    info = {"type": "service_account", "project_id": "sheerwater", "client_email": "x@y"}
    monkeypatch.setenv("NEURAL_GCM_SERVICE_CREDENTIALS", json.dumps(info))
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    assert fetch_mod._filesystem() == "fake-fs"
    assert seen == {"token": info}


def test_filesystem_rejects_invalid_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.setenv("NEURAL_GCM_SERVICE_CREDENTIALS", "{not valid json")
    with pytest.raises(fetch_mod.DataError, match="not valid JSON"):
        fetch_mod._filesystem()


def test_filesystem_falls_back_without_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.delenv("NEURAL_GCM_SERVICE_CREDENTIALS", raising=False)
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    assert fetch_mod._filesystem() == "fake-fs"
    assert seen == {}

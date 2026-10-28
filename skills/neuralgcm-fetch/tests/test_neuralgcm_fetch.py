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


def test_missing_date_exits_1(tmp_path, fetch_mod, monkeypatch, capsys):
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: False)
    stamps = ["20260904T00", "20260907T00", "20260907T12", "20260911T00", "20260914T12"]
    monkeypatch.setattr(fetch_mod, "_list_init_stamps", lambda: stamps)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2026-09-08",
            "-o",
            str(tmp_path / "unused.zarr"),
        )
    assert exc.value.code == 1
    # One failed --date lists the inits around it, so the model need not guess again.
    assert (
        "Nearest published inits: 2026-09-04 00Z, 2026-09-07 00Z, 2026-09-07 12Z, "
        "2026-09-11 00Z, 2026-09-14 12Z (5 inits from 2026-09-04 to 2026-09-14)"
    ) in capsys.readouterr().err


def test_filesystem_uses_credentials_json_when_set(fetch_mod, monkeypatch):
    info = {"type": "service_account", "project_id": "sheerwater", "client_email": "x@y"}
    monkeypatch.setenv("GCLOUD_SERVICE_CREDENTIALS", json.dumps(info))
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    assert fetch_mod._filesystem() == "fake-fs"
    assert seen == {"token": info}


def test_filesystem_rejects_invalid_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.setenv("GCLOUD_SERVICE_CREDENTIALS", "{not valid json")
    with pytest.raises(fetch_mod.DataError, match="not valid JSON"):
        fetch_mod._filesystem()


def test_filesystem_falls_back_without_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.delenv("GCLOUD_SERVICE_CREDENTIALS", raising=False)
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    assert fetch_mod._filesystem() == "fake-fs"
    assert seen == {}


def test_temperature_steps_keep_valid_lead(tmp_path, fetch_mod, monkeypatch):
    """Temperatures are instants at init + timedelta: the first lead stays 6 h."""
    out = tmp_path / "neuralgcm_t2m.zarr"
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *a, **k: _native_surface())

    run_skill(fetch_mod.fetch, "--dataset", "surface", "--date", "2026-09-11", "-o", str(out))

    with xr.open_zarr(out, consolidated=True) as ds:
        hours = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
        assert list(hours) == [6, 12, 18]


def test_read_failure_is_data_error(tmp_path, fetch_mod, monkeypatch, capsys):
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *a, **k: _native_precip())

    def broken_load(self, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(xr.Dataset, "load", broken_load)
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--date", "2026-09-11", "-o", str(tmp_path / "x.zarr"))
    assert exc.value.code == 1
    assert "failed to read gs://neuralgcm-s2s" in capsys.readouterr().err


def test_filesystem_rejects_non_object_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.setenv("GCLOUD_SERVICE_CREDENTIALS", '"not-a-key"')
    monkeypatch.setattr(gcsfs, "GCSFileSystem", lambda **kwargs: pytest.fail("token passed on"))
    with pytest.raises(fetch_mod.DataError, match="JSON object"):
        fetch_mod._filesystem()


_WINDOW_STAMPS = ["20250130T00", "20250131T00", "20250201T00", "20250201T12", "20260115T00"]


def _mock_windows(monkeypatch, fetch_mod, *, missing=(), opened=None):
    """Per-init stores whose tp is the init's day of month (in m per 6 h, x 1e-3)."""

    def fake_open(stamp, dataset):
        if opened is not None:
            opened.append(stamp)
        ds = _native_precip(fill=int(stamp[6:8]) * 1e-3)
        init = np.datetime64(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[-2:]}", "ns")
        return ds.assign_coords(init_time=[init])

    monkeypatch.setattr(fetch_mod, "_list_init_stamps", lambda: list(_WINDOW_STAMPS))
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, dataset: stamp not in missing)
    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)


def test_init_window_keeps_00utc_inits_on_a_time_axis(tmp_path, fetch_mod, monkeypatch):
    opened = []
    _mock_windows(monkeypatch, fetch_mod, opened=opened)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch, "--start-time", "2025-01-31", "--end-time", "2025-02-01", "-o", str(out)
    )
    # 20250201T12 is not a 00 UTC init.
    assert opened == ["20250131T00", "20250201T00"]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["tp"].dims == ("time", "step", "number", "latitude", "longitude")
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
        assert days == ["2025-01-31", "2025-02-01"]
        # Day 31 → 0.031 m / 6 h = 124 mm day-1; day 1 → 4 mm day-1.
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0, 0, 0], [124.0, 4.0], rtol=1e-5)
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


def test_init_window_warns_on_missing_and_short_inits(tmp_path, fetch_mod, monkeypatch, capsys):
    _mock_windows(monkeypatch, fetch_mod, missing={"20250131T00"})
    fake_open = fetch_mod._open_remote

    def short_newest(stamp, dataset):
        ds = fake_open(stamp, dataset)
        if stamp == "20250201T00":
            ds["total_precipitation_6hr"][:, :, 2:] = np.nan
        return ds

    monkeypatch.setattr(fetch_mod, "_open_remote", short_newest)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch, "--start-time", "2025-01-30", "--end-time", "2025-02-01", "-o", str(out)
    )
    err = capsys.readouterr().err
    assert "init 20250131T00 has no 'imerg:precip' store" in err
    assert "2025-02-01T00 UTC init has no data after step 6 h" in err
    with xr.open_zarr(out, consolidated=True) as ds:
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
        assert days == ["2025-01-30", "2025-02-01"]
        # The short init's late leads stay NaN; the other keeps every lead.
        assert ds["tp"].isel(time=1, step=slice(2, None)).isnull().all()
        assert ds["tp"].isel(time=0).notnull().all()


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

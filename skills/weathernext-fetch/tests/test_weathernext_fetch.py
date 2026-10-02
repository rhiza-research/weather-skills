"""Correctness tests for weathernext-fetch (network / remote Zarr mocked)."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("weathernext-fetch", "fetch")


def _native_predictions(*, fill=0.001, t2m_fill=280.0, n_step=4, members=2, with_level=False):
    """Source-like cube: raw undecoded ``time`` (int64 ns), no attrs anywhere."""
    lats = np.array([0.0, 2.0], dtype="float32")
    lons = np.array([10.0, 11.0], dtype="float32")
    step_hours = np.array([6 * (i + 1) for i in range(n_step)])
    step_ns = (step_hours.astype("int64") * 3600 * 1_000_000_000).astype("int64")
    init = np.datetime64("2026-09-25T00:00:00", "ns")
    datetime_vals = (init + step_hours.astype("timedelta64[h]")).astype("datetime64[ns]")
    shape = (members, n_step, len(lats), len(lons))

    data_vars = {
        "total_precipitation_6hr": (
            ("sample", "time", "lat", "lon"),
            np.full(shape, fill, dtype=np.float32),
        ),
        "2m_temperature": (
            ("sample", "time", "lat", "lon"),
            np.full(shape, t2m_fill, dtype=np.float32),
        ),
    }
    coords = {
        "sample": np.arange(members),
        "time": step_ns,
        "datetime": ("time", datetime_vals),
        "init_time": init,
        "lat": lats,
        "lon": lons,
    }
    if with_level:
        levels = np.array([500, 850], dtype="int32")
        level_shape = (members, n_step, len(levels), len(lats), len(lons))
        data_vars["temperature"] = (
            ("sample", "time", "level", "lat", "lon"),
            np.full(level_shape, 280.0, dtype=np.float32),
        )
        coords["level"] = levels
    return xr.Dataset(data_vars, coords=coords)


def test_stamp_to_date(fetch_mod):
    assert fetch_mod._stamp_to_date("20260925_00hr_01_preds") == "2026-09-25"
    assert fetch_mod._stamp_to_date("20260925_12hr_01_preds") == "2026-09-25"


def test_fetch_writes_zarr_and_stamps_history(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "weathernext.zarr"
    remote = _native_predictions()
    seen = {}

    monkeypatch.setattr(
        fetch_mod,
        "_list_init_stamps",
        lambda: [
            "20260924_18hr_01_preds",
            "20260925_00hr_01_preds",
            "20260925_06hr_01_preds",
        ],
    )
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp: stamp == "20260925_06hr_01_preds")

    def fake_open(stamp):
        seen["stamp"] = stamp
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)

    run_skill(fetch_mod.fetch, "-o", str(out))

    assert seen["stamp"] == "20260925_06hr_01_preds"
    assert Path(out).exists()
    with xr.open_zarr(out, consolidated=True) as ds:
        assert "tp" in ds.data_vars
        assert "total_precipitation_6hr" not in ds.data_vars
        assert {"number", "step", "lat", "lon"} <= set(ds.sizes)
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds["tp"].attrs.get("data_interval")
        hours = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
        assert list(hours) == [0, 6, 12, 18]
        # 0.001 m / 6h = 4 mm day-1
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [4.0, 4.0, 4.0, 4.0], rtol=1e-5)
        assert ds["2m_temperature"].attrs["units"] == "degree_Celsius"
        np.testing.assert_allclose(ds["2m_temperature"].values.mean(), 6.85, atol=0.01)
        assert "weathernext:20260925_06hr_01_preds" in ds.attrs.get("weather_skills_source", "")
        assert "aggregation_period" not in ds["tp"].attrs
        assert ds["valid_time"].dims == ("step",)
    history = load_history(out)
    assert history[-1]["skill"] == "weathernext-fetch"


def test_fetch_honors_date_cycle_variable_and_bbox(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "weathernext_bbox.zarr"
    remote = _native_predictions()
    seen = {}

    def fake_exists(stamp):
        seen["key"] = stamp
        return True

    monkeypatch.setattr(fetch_mod, "_store_exists", fake_exists)

    def fake_open(stamp):
        seen["open"] = stamp
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)

    run_skill(
        fetch_mod.fetch,
        "--date",
        "2026-09-25",
        "--cycle",
        "12",
        "-v",
        "t2m",
        "--bbox",
        "3/9/0/12",
        "-o",
        str(out),
    )

    assert seen["key"] == "20260925_12hr_01_preds"
    assert seen["open"] == "20260925_12hr_01_preds"
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.data_vars) == ["2m_temperature"]
    assert ds["2m_temperature"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(ds["2m_temperature"].values.mean(), 6.85, atol=0.01)


def test_fetch_stamps_level_coord(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "weathernext_level.zarr"
    remote = _native_predictions(with_level=True)

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))

    run_skill(
        fetch_mod.fetch,
        "--date",
        "2026-09-25",
        "-v",
        "t",
        "-o",
        str(out),
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.data_vars) == ["temperature"]
    assert ds["level"].attrs["standard_name"] == "air_pressure"
    assert units_equal(ds["level"].attrs["units"], "hPa")
    assert ds["temperature"].attrs["units"] == "degree_Celsius"


def test_fetch_member_selection(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "weathernext_member.zarr"
    remote = _native_predictions(members=4)

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))

    run_skill(
        fetch_mod.fetch,
        "--date",
        "2026-09-25",
        "-v",
        "t2m",
        "--member",
        "1",
        "--member",
        "3",
        "-o",
        str(out),
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds["number"].values) == [1, 3]


def test_fetch_member_out_of_range_exits_2(tmp_path, fetch_mod, monkeypatch):
    remote = _native_predictions(members=4)
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2026-09-25",
            "-v",
            "t2m",
            "--member",
            "9",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_probe_latest(capsys, fetch_mod, monkeypatch):
    monkeypatch.setattr(
        fetch_mod,
        "_list_init_stamps",
        lambda: [
            "20260924_18hr_01_preds",
            "20260925_00hr_01_preds",
            "20260925_06hr_01_preds",
        ],
    )
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp: stamp.endswith("_06hr_01_preds"))
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-09-25"


def test_unknown_variable_exits_2(tmp_path, fetch_mod, monkeypatch):
    remote = _native_predictions()
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2026-09-25",
            "-v",
            "bogus",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_missing_date_exits_1(tmp_path, fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp: False)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2020-01-01",
            "-o",
            str(tmp_path / "unused.zarr"),
        )
    assert exc.value.code == 1

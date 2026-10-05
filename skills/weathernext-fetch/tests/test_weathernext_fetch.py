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
        lambda *args: [
            "20260924_18hr_01_preds",
            "20260925_00hr_01_preds",
            "20260925_06hr_01_preds",
        ],
    )
    monkeypatch.setattr(
        fetch_mod, "_store_exists", lambda stamp, *args: stamp == "20260925_06hr_01_preds"
    )

    def fake_open(stamp, *args):
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

    def fake_exists(stamp, *args):
        seen["key"] = stamp
        return True

    monkeypatch.setattr(fetch_mod, "_store_exists", fake_exists)

    def fake_open(stamp, *args):
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

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
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

    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
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
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
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
        lambda *args: [
            "20260924_18hr_01_preds",
            "20260925_00hr_01_preds",
            "20260925_06hr_01_preds",
        ],
    )
    monkeypatch.setattr(
        fetch_mod, "_store_exists", lambda stamp, *args: stamp.endswith("_06hr_01_preds")
    )
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-09-25"


def test_unknown_variable_exits_2(tmp_path, fetch_mod, monkeypatch):
    remote = _native_predictions()
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
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
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: False)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2020-01-01",
            "-o",
            str(tmp_path / "unused.zarr"),
        )
    assert exc.value.code == 1


# --- WeatherNext 3 -----------------------------------------------------------


def _native_v3_ensemble(*, tp_fill=0.001, members=2, n_lead=2):
    """WN3-like cube: 6h ``lead_time`` x hourly ``lead_subtime``, multi-grid, 0..360 lon."""
    lat1 = np.array([0.0, 0.1], dtype="float32")
    lon1 = np.array([10.0, 10.1], dtype="float32")
    lat25 = np.array([0.0, 0.25], dtype="float32")
    lon25 = np.array([10.0, 10.25], dtype="float32")
    lead_time = np.array([6 * (i + 1) for i in range(n_lead)], dtype="int64")
    subtime = (np.arange(-5, 1) * np.timedelta64(1, "h")).astype("timedelta64[ns]")
    init = np.datetime64("2026-10-02T00:00:00", "ns")
    surf = ("sample", "lead_time", "lead_subtime", "lat_0p1", "lon_0p1")
    surf_shape = (members, n_lead, 6, 2, 2)
    plev_shape = (members, n_lead, 2, 2, 2)
    return xr.Dataset(
        {
            "total_precipitation_1hr": (surf, np.full(surf_shape, tp_fill, dtype=np.float32)),
            "temperature_2m": (surf, np.full(surf_shape, 280.0, dtype=np.float32)),
            "mean_sea_level_pressure": (surf, np.full(surf_shape, 101325.0, dtype=np.float32)),
            "temperature": (
                ("sample", "lead_time", "level", "lat_0p25", "lon_0p25"),
                np.full(plev_shape, 250.0, dtype=np.float32),
            ),
        },
        coords={
            "sample": np.arange(members),
            "lead_time": lead_time,
            "lead_subtime": subtime,
            "level": np.array([500, 850], dtype="int32"),
            "datetime": ("lead_time", init + lead_time.astype("timedelta64[h]")),
            "init_time": init,
            "lat_0p1": lat1,
            "lon_0p1": lon1,
            "lat_0p25": lat25,
            "lon_0p25": lon25,
        },
    )


def _native_v3_statistics(n_lead=3):
    lats = np.array([0.0, 0.1], dtype="float32")
    lons = np.array([10.0, 10.1], dtype="float32")
    data_vars = {}
    for stat in ("mean", "p10", "p50", "p90"):
        data_vars[f"temperature_2m_{stat}"] = (
            ("lead_time", "lat_0p1", "lon_0p1"),
            np.full((n_lead, 2, 2), 280.0, dtype=np.float32),
        )
        data_vars[f"total_precipitation_1hr_{stat}"] = (
            ("lead_time", "lat_0p1", "lon_0p1"),
            np.full((n_lead, 2, 2), 0.002, dtype=np.float32),
        )
    return xr.Dataset(
        data_vars,
        coords={
            "lead_time": np.arange(1, n_lead + 1, dtype="int64"),
            "init_time": np.datetime64("2026-10-02T00:00:00", "ns"),
            "lat_0p1": lats,
            "lon_0p1": lons,
        },
    )


def _mock_v3(monkeypatch, fetch_mod, remote, seen=None):
    seen = {} if seen is None else seen

    def fake_exists(stamp, product, billing_project=None):
        seen.setdefault("exists", []).append((stamp, product.bucket, billing_project))
        return True

    def fake_open(stamp, product, billing_project=None):
        seen["open"] = (stamp, product.bucket, billing_project)
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_store_exists", fake_exists)
    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)
    return seen


def test_v3_ensemble_flattens_hourly_leads(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "wn3.zarr"
    seen = _mock_v3(monkeypatch, fetch_mod, _native_v3_ensemble())
    run_skill(
        fetch_mod.fetch,
        "--version", "3",
        "--date", "2026-10-02",
        "--billing-project", "my-proj",
        "-v", "tp", "-v", "t2m",
        "-o", str(out),
    )  # fmt: skip

    assert seen["open"] == ("20261002_00hr_01_preds", "weathernext3_spatial", "my-proj")
    with xr.open_zarr(out, consolidated=True) as ds:
        assert set(ds.data_vars) == {"tp", "2m_temperature"}
        assert {"number", "step", "lat", "lon"} == set(ds.sizes)
        hours = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
        assert list(hours) == list(range(12))
        valid = ds["valid_time"].values
        assert valid[0] == np.datetime64("2026-10-02T01:00:00", "ns")
        assert valid[-1] == np.datetime64("2026-10-02T12:00:00", "ns")
        assert ds["tp"].attrs["units"] == "mm day-1"
        # 0.001 m / 1h = 24 mm day-1
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], 24.0, rtol=1e-5)
        assert ds["2m_temperature"].attrs["units"] == "degree_Celsius"
        assert ds.attrs["weather_skills_source"] == "weathernext3:20261002_00hr_01_preds"


def test_v3_pressure_levels_are_6_hourly_on_025(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "wn3_t.zarr"
    _mock_v3(monkeypatch, fetch_mod, _native_v3_ensemble())
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
    run_skill(fetch_mod.fetch, "--version", "3", "--date", "2026-10-02", "-v", "t", "-o", str(out))
    with xr.open_zarr(out, consolidated=True) as ds:
        assert list(ds.data_vars) == ["temperature"]
        assert "level" in ds.dims
        hours = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
        assert list(hours) == [0, 6]
        np.testing.assert_allclose(ds["lat"].values, [0.0, 0.25])


def test_v3_mixed_grids_exit_2(tmp_path, fetch_mod, monkeypatch):
    _mock_v3(monkeypatch, fetch_mod, _native_v3_ensemble())
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--version", "3",
            "--date", "2026-10-02",
            "--billing-project", "p",
            "-v", "t2m", "-v", "t",
            "-o", str(tmp_path / "out.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_v3_default_variables_are_01deg_surface(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "wn3_all.zarr"
    _mock_v3(monkeypatch, fetch_mod, _native_v3_ensemble())
    run_skill(
        fetch_mod.fetch,
        "--version", "3",
        "--date", "2026-10-02",
        "--billing-project", "p",
        "--member", "1",
        "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        assert set(ds.data_vars) == {"tp", "2m_temperature", "mean_sea_level_pressure"}
        assert list(ds["number"].values) == [1]


def test_v3_ensemble_requires_billing_project(tmp_path, fetch_mod, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--version",
            "3",
            "--date",
            "2026-10-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 2


def test_v3_statistics(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "wn3_stats.zarr"
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)
    seen = _mock_v3(monkeypatch, fetch_mod, _native_v3_statistics())
    run_skill(
        fetch_mod.fetch,
        "--version", "3",
        "--product", "statistics",
        "--date", "2026-10-02",
        "-v", "tp", "-v", "t2m",
        "--statistic", "mean", "--statistic", "p90",
        "-o", str(out),
    )  # fmt: skip
    assert seen["open"] == ("20261002_00hr_01_preds", "weathernext3_statistics_spatial", None)
    with xr.open_zarr(out, consolidated=True) as ds:
        assert set(ds.data_vars) == {
            "tp_mean",
            "tp_p90",
            "2m_temperature_mean",
            "2m_temperature_p90",
        }
        assert "number" not in ds.dims
        hours = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
        assert list(hours) == [0, 1, 2]
        np.testing.assert_allclose(ds["tp_mean"].values, 48.0, rtol=1e-5)
        assert ds["tp_p90"].attrs["units"] == "mm day-1"
        assert ds["2m_temperature_p90"].attrs["units"] == "degree_Celsius"
        assert "90th percentile" in ds["2m_temperature_p90"].attrs["long_name"]
        assert ds.attrs["weather_skills_source"].startswith("weathernext3-statistics:")


@pytest.mark.parametrize(
    "argv",
    [
        ["--product", "statistics"],  # v2 has no statistics product
        ["--cycle", "07"],  # v2 has no interim inits
        ["--version", "3", "--product", "statistics", "--member", "0"],
        ["--version", "3", "--billing-project", "p", "--statistic", "mean"],
    ],
)
def test_invalid_flag_combinations_exit_2(tmp_path, fetch_mod, monkeypatch, argv):
    _mock_v3(monkeypatch, fetch_mod, _native_v3_statistics())
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, *argv, "--date", "2026-10-02", "-o", str(tmp_path / "o.zarr"))
    assert exc.value.code == 2


def test_v3_probe_latest_skips_interim_inits_by_default(capsys, fetch_mod, monkeypatch):
    stamps = ["20261001_18hr_01_preds", "20261002_00hr_01_preds", "20261002_01hr_01_preds"]
    monkeypatch.setattr(fetch_mod, "_list_init_stamps", lambda *args: list(stamps))
    seen = []

    def fake_exists(stamp, *args):
        seen.append(stamp)
        return True

    monkeypatch.setattr(fetch_mod, "_store_exists", fake_exists)
    run_skill(fetch_mod.fetch, "--version", "3", "--product", "statistics", "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-10-02"
    assert seen == ["20261002_00hr_01_preds"]

    seen.clear()
    run_skill(
        fetch_mod.fetch,
        "--version",
        "3",
        "--product",
        "statistics",
        "--cycle",
        "01",
        "--probe-latest",
    )
    assert seen == ["20261002_01hr_01_preds"]


def test_max_lead_trims_before_load(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "wn3_short.zarr"
    _mock_v3(monkeypatch, fetch_mod, _native_v3_ensemble())
    run_skill(
        fetch_mod.fetch,
        "--version", "3",
        "--date", "2026-10-02",
        "--billing-project", "p",
        "-v", "t2m",
        "--max-lead", "8",
        "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        hours = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
        assert list(hours) == list(range(8))  # leads 1..8h, left-labeled

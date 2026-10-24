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


def _lead_hours(ds):
    return list(np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int))


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
        # tp moves to period start (0..18 h); t2m keeps its native leads (6..24 h).
        assert _lead_hours(ds) == [0, 6, 12, 18, 24]
        # 0.001 m / 6h = 4 mm day-1; no period after the last lead.
        np.testing.assert_allclose(
            ds["tp"].values[0, :, 0, 0], [4.0, 4.0, 4.0, 4.0, np.nan], rtol=1e-5
        )
        assert ds["2m_temperature"].attrs["units"] == "degree_Celsius"
        assert np.isnan(ds["2m_temperature"].values[0, 0, 0, 0])
        np.testing.assert_allclose(np.nanmean(ds["2m_temperature"].values), 6.85, atol=0.01)
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


def test_t2m_keeps_native_lead_and_tp_moves_to_period_start(tmp_path, fetch_mod, monkeypatch):
    """t2m is instantaneous at init + step; tp is a 6 h total labelled at its end."""
    remote = _native_predictions(members=1)
    hours = np.array([6.0, 12.0, 18.0, 24.0], dtype="float32")
    remote["2m_temperature"].values[:] = (273.15 + hours)[None, :, None, None]  # degC == lead
    remote["total_precipitation_6hr"].values[:] = (hours / 1000.0)[None, :, None, None]
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    out = tmp_path / "mixed.zarr"
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", "-v", "tp", "-v", "t2m", "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True).isel(number=0, lat=0, lon=0)
    t2m = next(v for v in ds.data_vars if v != "tp")
    lead_h = ds["step"].values / np.timedelta64(1, "h")
    np.testing.assert_allclose(lead_h, [0, 6, 12, 18, 24])
    # t2m sits on its own valid time: the 6 h value at step 6 h, nothing at 0.
    np.testing.assert_allclose(ds[t2m].values, [np.nan, 6, 12, 18, 24], rtol=1e-5)
    np.testing.assert_array_equal(ds["valid_time"].values, ds["time"].values + ds["step"].values)
    # tp's 0-6 h total is labelled at the period start, nothing after the last period.
    assert np.isnan(ds["tp"].values[-1]) and not np.isnan(ds["tp"].values[0])
    assert ds["tp"].values[0] < ds["tp"].values[1]


def test_t2m_alone_keeps_native_leads(tmp_path, fetch_mod, monkeypatch):
    remote = _native_predictions(members=1)
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    out = tmp_path / "t2m.zarr"
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", "-v", "t2m", "-o", str(out))
    lead_h = xr.open_zarr(out, consolidated=True)["step"].values / np.timedelta64(1, "h")
    np.testing.assert_allclose(lead_h, [6, 12, 18, 24])


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
        # tp moves to period start (0..11 h); t2m keeps its native leads (1..12 h).
        assert _lead_hours(ds) == list(range(13))
        valid = ds["valid_time"].values
        assert valid[0] == np.datetime64("2026-10-02T00:00:00", "ns")
        assert valid[-1] == np.datetime64("2026-10-02T12:00:00", "ns")
        assert ds["tp"].attrs["units"] == "mm day-1"
        # 0.001 m / 1h = 24 mm day-1; no period after the last lead.
        np.testing.assert_allclose(ds["tp"].values[0, :-1, 0, 0], 24.0, rtol=1e-5)
        assert np.isnan(ds["tp"].values[0, -1, 0, 0])
        assert np.isnan(ds["2m_temperature"].values[0, 0, 0, 0])
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
        assert _lead_hours(ds) == [6, 12]
        np.testing.assert_allclose(ds["lat"].values, [0.0, 0.25])


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
        assert _lead_hours(ds) == [0, 1, 2, 3]
        np.testing.assert_allclose(ds["tp_mean"].values[:-1], 48.0, rtol=1e-5)
        assert np.isnan(ds["tp_mean"].values[-1]).all()
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
        ["--version", "3"],  # requester-pays ensemble without a billing project
        ["--version", "3", "--billing-project", "p", "-v", "t2m", "-v", "t"],  # mixed grids
    ],
)
def test_invalid_flag_combinations_exit_2(tmp_path, fetch_mod, monkeypatch, argv):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)
    _mock_v3(monkeypatch, fetch_mod, _native_v3_ensemble())
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
        fetch_mod.fetch, "--version", "3", "--product", "statistics", "--cycle", "01", "--probe-latest"
    )  # fmt: skip
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
        assert _lead_hours(ds) == list(range(1, 9))  # t2m keeps its native 1..8h leads


def test_v3_reads_store_without_dask(tmp_path, fetch_mod, monkeypatch):
    """The real open path reads a Zarr lazily without dask, subsets, then flattens."""
    import fsspec

    store = tmp_path / "predictions.zarr"
    _native_v3_ensemble(n_lead=3).to_zarr(store, consolidated=True)
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda *args: True)
    monkeypatch.setattr(fetch_mod, "_store_url", lambda *args: str(store))
    monkeypatch.setattr(fetch_mod, "_filesystem", lambda *args: fsspec.filesystem("file"))
    out = tmp_path / "wn3_disk.zarr"
    run_skill(
        fetch_mod.fetch,
        "--version", "3",
        "--date", "2026-10-02",
        "--billing-project", "p",
        "--bbox", "0.05/9/-1/10.05",
        "-v", "t2m",
        "--member", "1",
        "--max-lead", "8",
        "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        assert _lead_hours(ds) == list(range(1, 9))
        assert ds.sizes["lat"] == 1 and ds.sizes["lon"] == 1
        assert list(ds["number"].values) == [1]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-v", "tp_p90"], {"tp_p90"}),
        (["-v", "total_precipitation_1hr_p90"], {"tp_p90"}),
        (["-v", "tp_p90", "-v", "t2m_mean"], {"tp_p90", "2m_temperature_mean"}),
        (["-v", "tp_p90", "-v", "tp"], {"tp_mean", "tp_p10", "tp_p50", "tp_p90"}),
        (["-v", "tp_p90", "-v", "t2m", "--statistic", "p90"], {"tp_p90", "2m_temperature_p90"}),
    ],
)
def test_v3_variable_naming_a_statistic_keeps_only_it(
    tmp_path, fetch_mod, monkeypatch, argv, expected
):
    out = tmp_path / "wn3_stat.zarr"
    _mock_v3(monkeypatch, fetch_mod, _native_v3_statistics())
    run_skill(
        fetch_mod.fetch,
        "--version", "3", "--product", "statistics", "--date", "2026-10-02",
        *argv,
        "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        assert set(ds.data_vars) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["--product", "statistics", "-v", "tp_p90", "--statistic", "mean"],  # conflict
        ["--product", "statistics", "-v", "tp_p25"],  # statistic not in the store
        ["--billing-project", "p", "-v", "tp_p90"],  # ensemble has no statistics
    ],
)
def test_v3_bad_statistic_variable_exits_2(tmp_path, fetch_mod, monkeypatch, argv):
    remote = _native_v3_statistics() if "statistics" in argv else _native_v3_ensemble()
    _mock_v3(monkeypatch, fetch_mod, remote)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch, "--version", "3", "--date", "2026-10-02", *argv,
            "-o", str(tmp_path / "o.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_v3_solar_radiation_moves_to_period_start_in_j_m2(tmp_path, fetch_mod, monkeypatch):
    """ssrd is a 1 h total ending at init + step, like tp: relabelled to its start."""
    remote = _native_v3_ensemble(members=1)
    dims = remote["temperature_2m"].dims
    remote["surface_solar_radiation_downwards_1hr"] = (
        dims,
        np.full(remote["temperature_2m"].shape, 3.6e6, dtype=np.float32),
    )
    _mock_v3(monkeypatch, fetch_mod, remote)
    out = tmp_path / "wn3_ssrd.zarr"
    run_skill(
        fetch_mod.fetch,
        "--version", "3", "--date", "2026-10-02", "--billing-project", "p",
        "-v", "ssrd", "-v", "t2m",
        "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        ssrd = ds["surface_solar_radiation_downwards_1hr"]
        assert _lead_hours(ds) == list(range(13))
        assert ssrd.attrs["units"] == "J m-2"
        assert ssrd.attrs["standard_name"].startswith("integral_wrt_time_of_")
        # The 0-1 h total sits at step 0; nothing after the last period.
        np.testing.assert_allclose(ssrd.values[0, :-1], 3.6e6, rtol=1e-6)
        assert np.isnan(ssrd.values[0, -1]).all()
        assert np.isnan(ds["2m_temperature"].values[0, 0]).all()


def test_max_lead_and_cycle_accept_both_spellings(tmp_path, fetch_mod, monkeypatch):
    seen = _mock_v3(monkeypatch, fetch_mod, _native_v3_ensemble())
    for max_lead in ("8", "8h"):
        out = tmp_path / f"wn3_{max_lead}.zarr"
        run_skill(
            fetch_mod.fetch,
            "--version", "3", "--date", "2026-10-02", "--cycle", "6",
            "--billing-project", "p", "-v", "t2m", "--max-lead", max_lead,
            "-o", str(out),
        )  # fmt: skip
        assert seen["open"][0] == "20261002_06hr_01_preds"
        with xr.open_zarr(out, consolidated=True) as ds:
            assert _lead_hours(ds) == list(range(1, 9))
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch, "--version", "3", "--date", "2026-10-02", "--billing-project", "p",
            "-v", "t2m", "--max-lead", "soon", "-o", str(tmp_path / "o.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_precip_fields_stay_float32(tmp_path, fetch_mod, monkeypatch):
    # v3 ensemble: tp plus the IMERG-calibrated and experimental precip fields.
    remote = _native_v3_ensemble()
    for native in ("imerg_tp_1hr", "experimental_tp_1hr"):
        remote[native] = remote["total_precipitation_1hr"]
    _mock_v3(monkeypatch, fetch_mod, remote)
    out = tmp_path / "wn3.zarr"
    run_skill(
        fetch_mod.fetch, "--version", "3", "--date", "2026-10-02", "--billing-project", "p",
        "-v", "tp", "-v", "imerg_tp", "-v", "experimental_tp", "-v", "t2m", "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        assert {n: str(ds[n].dtype) for n in ds.data_vars} == {
            "tp": "float32",
            "imerg_tp": "float32",
            "experimental_tp": "float32",
            "2m_temperature": "float32",
        }
        np.testing.assert_allclose(ds["imerg_tp"].values[0, :-1, 0, 0], 24.0, rtol=1e-5)

    # v3 statistics: per-statistic precip names.
    _mock_v3(monkeypatch, fetch_mod, _native_v3_statistics())
    out = tmp_path / "wn3_stats.zarr"
    run_skill(
        fetch_mod.fetch, "--version", "3", "--product", "statistics", "--date", "2026-10-02",
        "-v", "tp", "--statistic", "mean", "--statistic", "p90", "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["tp_mean"].dtype == np.float32 and ds["tp_p90"].dtype == np.float32


def test_filesystem_uses_credentials_json(fetch_mod, monkeypatch):
    import json

    import gcsfs

    info = {"type": "service_account", "project_id": "p"}
    monkeypatch.setenv("GCLOUD_SERVICE_CREDENTIALS", json.dumps(info))
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    product = fetch_mod._resolve_product("3", None)
    assert fetch_mod._filesystem(product, "bill") == "fake-fs"
    assert seen == {"requester_pays": "bill", "token": info}


def test_filesystem_rejects_invalid_credentials_json(fetch_mod, monkeypatch):
    monkeypatch.setenv("GCLOUD_SERVICE_CREDENTIALS", "{not valid json")
    with pytest.raises(fetch_mod.DataError, match="not valid JSON"):
        fetch_mod._filesystem(fetch_mod._resolve_product("2", None))


def test_filesystem_falls_back_without_credentials_json(fetch_mod, monkeypatch):
    import gcsfs

    monkeypatch.delenv("GCLOUD_SERVICE_CREDENTIALS", raising=False)
    seen = {}

    def fake_gcs_filesystem(**kwargs):
        seen.update(kwargs)
        return "fake-fs"

    monkeypatch.setattr(gcsfs, "GCSFileSystem", fake_gcs_filesystem)
    assert fetch_mod._filesystem(fetch_mod._resolve_product("2", None)) == "fake-fs"
    assert seen == {}

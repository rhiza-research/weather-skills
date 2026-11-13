"""Correctness tests for weathernext-fetch (network / remote Zarr mocked)."""

import sys
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


class _NoCatalog:
    """dynamical_catalog stand-in that is unreachable: WeatherNext 2 falls back to GCS."""

    @staticmethod
    def list():
        raise ConnectionError("catalog unreachable (test stub)")


@pytest.fixture(autouse=True)
def _no_live_catalog(monkeypatch):
    monkeypatch.setitem(sys.modules, "dynamical_catalog", _NoCatalog)


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
        assert "precip" in ds.data_vars
        assert "total_precipitation_6hr" not in ds.data_vars
        assert {"number", "step", "lat", "lon"} <= set(ds.sizes)
        assert ds["precip"].attrs["units"] == "mm day-1"
        assert ds["precip"].attrs.get("data_interval")
        # precip moves to period start (0..18 h); t2m keeps its native leads (6..24 h).
        assert _lead_hours(ds) == [0, 6, 12, 18, 24]
        # 0.001 m / 6h = 4 mm day-1; no period after the last lead.
        np.testing.assert_allclose(
            ds["precip"].values[0, :, 0, 0], [4.0, 4.0, 4.0, 4.0, np.nan], rtol=1e-5
        )
        assert ds["2m_temperature"].attrs["units"] == "degree_Celsius"
        assert np.isnan(ds["2m_temperature"].values[0, 0, 0, 0])
        np.testing.assert_allclose(np.nanmean(ds["2m_temperature"].values), 6.85, atol=0.01)
        assert "weathernext:20260925_06hr_01_preds" in ds.attrs.get("weather_skills_source", "")
        assert "aggregation_period" not in ds["precip"].attrs
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


def test_t2m_keeps_native_lead_and_precip_moves_to_period_start(tmp_path, fetch_mod, monkeypatch):
    """t2m is instantaneous at init + step; precip is a 6 h total labelled at its end."""
    remote = _native_predictions(members=1)
    hours = np.array([6.0, 12.0, 18.0, 24.0], dtype="float32")
    remote["2m_temperature"].values[:] = (273.15 + hours)[None, :, None, None]  # degC == lead
    remote["total_precipitation_6hr"].values[:] = (hours / 1000.0)[None, :, None, None]
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    out = tmp_path / "mixed.zarr"
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", "-v", "tp", "-v", "t2m", "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True).isel(number=0, lat=0, lon=0)
    t2m = next(v for v in ds.data_vars if v != "precip")
    lead_h = ds["step"].values / np.timedelta64(1, "h")
    np.testing.assert_allclose(lead_h, [0, 6, 12, 18, 24])
    # t2m sits on its own valid time: the 6 h value at step 6 h, nothing at 0.
    np.testing.assert_allclose(ds[t2m].values, [np.nan, 6, 12, 18, 24], rtol=1e-5)
    np.testing.assert_array_equal(ds["valid_time"].values, ds["time"].values + ds["step"].values)
    # precip's 0-6 h total is labelled at the period start, nothing after the last period.
    assert np.isnan(ds["precip"].values[-1]) and not np.isnan(ds["precip"].values[0])
    assert ds["precip"].values[0] < ds["precip"].values[1]


def test_t2m_alone_keeps_native_leads(tmp_path, fetch_mod, monkeypatch):
    remote = _native_predictions(members=1)
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    out = tmp_path / "t2m.zarr"
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", "-v", "t2m", "-o", str(out))
    lead_h = xr.open_zarr(out, consolidated=True)["step"].values / np.timedelta64(1, "h")
    np.testing.assert_allclose(lead_h, [6, 12, 18, 24])


def test_fetch_writes_lat_south_to_north(tmp_path, fetch_mod, monkeypatch):
    remote = _native_predictions(members=1).isel(lat=slice(None, None, -1))
    remote["2m_temperature"].values[..., 0, :] = 300.0  # the northern row, first in the store
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *args, **kwargs: remote.copy(deep=True))
    out = tmp_path / "t2m.zarr"
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", "-v", "t2m", "-o", str(out))
    written = xr.open_zarr(out, consolidated=True)
    np.testing.assert_allclose(written["lat"].values, [0.0, 2.0])
    t2m = written["2m_temperature"].isel(number=0, step=0)
    np.testing.assert_allclose(t2m.sel(lat=2.0).values, 26.85, atol=0.01)


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
        "-v", "precip", "-v", "t2m",
        "-o", str(out),
    )  # fmt: skip

    assert seen["open"] == ("20261002_00hr_01_preds", "weathernext3_spatial", "my-proj")
    with xr.open_zarr(out, consolidated=True) as ds:
        assert set(ds.data_vars) == {"precip", "2m_temperature"}
        assert {"number", "step", "lat", "lon"} == set(ds.sizes)
        # precip moves to period start (0..11 h); t2m keeps its native leads (1..12 h).
        assert _lead_hours(ds) == list(range(13))
        valid = ds["valid_time"].values
        assert valid[0] == np.datetime64("2026-10-02T00:00:00", "ns")
        assert valid[-1] == np.datetime64("2026-10-02T12:00:00", "ns")
        assert ds["precip"].attrs["units"] == "mm day-1"
        # 0.001 m / 1h = 24 mm day-1; no period after the last lead.
        np.testing.assert_allclose(ds["precip"].values[0, :-1, 0, 0], 24.0, rtol=1e-5)
        assert np.isnan(ds["precip"].values[0, -1, 0, 0])
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
        assert set(ds.data_vars) == {"precip", "2m_temperature", "mean_sea_level_pressure"}
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
        "-v", "precip", "-v", "t2m",
        "--statistic", "mean", "--statistic", "p90",
        "-o", str(out),
    )  # fmt: skip
    assert seen["open"] == ("20261002_00hr_01_preds", "weathernext3_statistics_spatial", None)
    with xr.open_zarr(out, consolidated=True) as ds:
        assert set(ds.data_vars) == {
            "precip_mean",
            "precip_p90",
            "2m_temperature_mean",
            "2m_temperature_p90",
        }
        assert "number" not in ds.dims
        assert _lead_hours(ds) == [0, 1, 2, 3]
        np.testing.assert_allclose(ds["precip_mean"].values[:-1], 48.0, rtol=1e-5)
        assert np.isnan(ds["precip_mean"].values[-1]).all()
        assert ds["precip_p90"].attrs["units"] == "mm day-1"
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
        (["-v", "precip_p90"], {"precip_p90"}),
        (["-v", "tp_p90"], {"precip_p90"}),
        (["-v", "precipitation"], {"precip_mean", "precip_p10", "precip_p50", "precip_p90"}),
        (["-v", "total_precipitation_1hr_p90"], {"precip_p90"}),
        (["-v", "precip_p90", "-v", "t2m_mean"], {"precip_p90", "2m_temperature_mean"}),
        (
            ["-v", "precip_p90", "-v", "precip"],
            {"precip_mean", "precip_p10", "precip_p50", "precip_p90"},
        ),
        (
            ["-v", "precip_p90", "-v", "t2m", "--statistic", "p90"],
            {"precip_p90", "2m_temperature_p90"},
        ),
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
        ["--product", "statistics", "-v", "precip_p90", "--statistic", "mean"],  # conflict
        ["--product", "statistics", "-v", "precip_p25"],  # statistic not in the store
        ["--billing-project", "p", "-v", "precip_p90"],  # ensemble has no statistics
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
    """ssrd is a 1 h total ending at init + step, like precip: relabelled to its start."""
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
    # v3 ensemble: precip plus the IMERG-calibrated and experimental precip fields.
    remote = _native_v3_ensemble()
    for native in ("imerg_tp_1hr", "experimental_tp_1hr"):
        remote[native] = remote["total_precipitation_1hr"]
    _mock_v3(monkeypatch, fetch_mod, remote)
    out = tmp_path / "wn3.zarr"
    run_skill(
        fetch_mod.fetch, "--version", "3", "--date", "2026-10-02", "--billing-project", "p",
        "-v", "precip", "-v", "imerg_precip", "-v", "experimental_precip", "-v", "t2m", "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        assert {n: str(ds[n].dtype) for n in ds.data_vars} == {
            "precip": "float32",
            "imerg_precip": "float32",
            "experimental_precip": "float32",
            "2m_temperature": "float32",
        }
        np.testing.assert_allclose(ds["imerg_precip"].values[0, :-1, 0, 0], 24.0, rtol=1e-5)

    # v3 statistics: per-statistic precip names.
    _mock_v3(monkeypatch, fetch_mod, _native_v3_statistics())
    out = tmp_path / "wn3_stats.zarr"
    run_skill(
        fetch_mod.fetch, "--version", "3", "--product", "statistics", "--date", "2026-10-02",
        "-v", "precip", "--statistic", "mean", "--statistic", "p90", "-o", str(out),
    )  # fmt: skip
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["precip_mean"].dtype == np.float32 and ds["precip_p90"].dtype == np.float32


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


_WINDOW_STAMPS = [
    "20250130_00hr_01_preds",
    "20250131_00hr_01_preds",
    "20250201_00hr_01_preds",
    "20250201_12hr_01_preds",
    "20260115_00hr_01_preds",
]


def _mock_windows(monkeypatch, fetch_mod, *, missing=(), short=(), listed=None):
    """Per-init v2 stores whose t2m is the init's day of month (K); ``short`` lacks leads 2+."""

    def fake_list(product, billing_project=None, day=None):
        if listed is not None:
            listed.append(day)
        return [s for s in _WINDOW_STAMPS if s.startswith(day or "")]

    def fake_open(stamp, *args):
        ds = _native_predictions(t2m_fill=float(stamp[6:8]))
        init = np.datetime64(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}", "ns")
        lead = ds["time"].values.astype("timedelta64[ns]")
        ds = ds.assign_coords(init_time=init, datetime=("time", init + lead))
        if stamp in short:
            ds["2m_temperature"][:, 2:] = np.nan
            ds["total_precipitation_6hr"][:, 2:] = np.nan
        return ds

    monkeypatch.setattr(fetch_mod, "_list_init_stamps", fake_list)
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: stamp not in missing)
    monkeypatch.setattr(fetch_mod, "_open_remote", fake_open)


def test_init_window_keeps_00utc_inits_on_a_time_axis(tmp_path, fetch_mod, monkeypatch):
    listed = []
    _mock_windows(monkeypatch, fetch_mod, listed=listed)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch,
        "--start-time",
        "2025-01-31",
        "--end-time",
        "2025-02-01",
        "-v",
        "t2m",
        "-o",
        str(out),
    )
    # One listing per month in the window.
    assert listed == ["202501", "202502"]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["2m_temperature"].dims == ("time", "step", "number", "lat", "lon")
        # 20250201_12hr is not a 00 UTC init.
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
        assert days == ["2025-01-31", "2025-02-01"]
        assert ds["valid_time"].dims == ("time", "step")
        # The 6 h lead spacing, not the 1-day gap between inits.
        assert ds["2m_temperature"].attrs["data_interval"] == "6 hour"
        np.testing.assert_allclose(
            ds["2m_temperature"].values[:, 0, 0, 0, 0], [31 - 273.15, 1 - 273.15], rtol=1e-5
        )
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
        "-v",
        "precip",
        "-o",
        str(out),
    )
    with xr.open_zarr(out, consolidated=True) as ds:
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2025-01-30", "2025-01-31", "2026-01-15"]


def test_init_window_warns_on_missing_and_short_inits(tmp_path, fetch_mod, monkeypatch, capsys):
    _mock_windows(
        monkeypatch,
        fetch_mod,
        missing={"20250131_00hr_01_preds"},
        short={"20250201_00hr_01_preds"},
    )
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch,
        "--start-time",
        "2025-01-30",
        "--end-time",
        "2025-02-01",
        "-v",
        "t2m",
        "-o",
        str(out),
    )
    err = capsys.readouterr().err
    assert "no complete store for init 20250131_00hr_01_preds" in err
    assert "2025-02-01T00 UTC init has no data after step 12 h" in err
    with xr.open_zarr(out, consolidated=True) as ds:
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
        assert days == ["2025-01-30", "2025-02-01"]
        assert ds["2m_temperature"].isel(time=1, step=slice(2, None)).isnull().all()
        assert ds["2m_temperature"].isel(time=0).notnull().all()


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


# --- WeatherNext 2 from the dynamical.org catalog -----------------------------


def _catalog_like(native, init="2026-09-25T00:00"):
    """The dynamical.org copy of ``native``: y/x, degC, kg m-2, an init_time axis."""
    lead = native["time"].values.astype("timedelta64[ns]")
    inits = np.array([init], dtype="datetime64[ns]")
    dims = ("init_time", "ensemble_member", "lead_time", "y", "x")
    attrs = {"comment": "catalog attrs are dropped", "step_type": "instant"}
    return xr.Dataset(
        {
            "total_precipitation_surface": (
                dims,
                native["total_precipitation_6hr"].values[None] * np.float32(1000),
                {"units": "kg m-2", "step_type": "accum"},
            ),
            "temperature_2m": (
                dims,
                native["2m_temperature"].values[None] - np.float32(273.15),
                {"units": "degree_Celsius", **attrs},
            ),
        },
        coords={
            "init_time": inits,
            "ensemble_member": native["sample"].values.astype("int16"),
            "lead_time": lead,
            "valid_time": (("init_time", "lead_time"), inits[:, None] + lead[None]),
            "expected_forecast_length": ("init_time", [np.timedelta64(360, "h")]),
            "y": native["lat"].values.astype("float64"),
            "x": native["lon"].values.astype("float64"),
            "spatial_ref": 0,
        },
        attrs={"dataset_id": "x", "license": "y"},
    )


def _mock_catalog(monkeypatch, ds, opened=None):
    """dynamical_catalog stand-in serving ``ds``; ``ds=None`` fails to read."""
    opened = [] if opened is None else opened

    class _Catalog:
        @staticmethod
        def list():
            return [
                "google-weathernext2-forecast-operational-virtual",
                "google-weathernext2-forecast-historical-virtual",
                *(
                    f"google-weathernext3-forecast-{length}-{res}-degree-virtual"
                    for length in ("15-day", "48-hour")
                    for res in ("0-1", "0-05")
                ),
            ]

        @staticmethod
        def open(dataset, **kwargs):
            opened.append(dataset)
            if ds is None:
                raise OSError("catalog read failed")
            return ds.copy(deep=True)

    monkeypatch.setitem(sys.modules, "dynamical_catalog", _Catalog)
    return opened


def _no_gcs(monkeypatch, fetch_mod):
    def boom(*args, **kwargs):
        raise AssertionError("GCS touched on the catalog path")

    for name in ("_filesystem", "_open_remote", "_store_exists", "_list_init_stamps"):
        monkeypatch.setattr(fetch_mod, name, boom)


def test_v2_00z_surface_fields_come_from_catalog_without_gcs(
    tmp_path, fetch_mod, monkeypatch, capsys
):
    opened = _mock_catalog(monkeypatch, _catalog_like(_native_predictions()))
    _no_gcs(monkeypatch, fetch_mod)
    out = tmp_path / "cat.zarr"
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", "-v", "tp", "-v", "t2m", "-o", str(out))
    assert opened == ["google-weathernext2-forecast-operational-virtual"]
    assert "Source: dynamical.org google-weathernext2-forecast-operational-virtual" in (
        capsys.readouterr().err
    )
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.attrs["weather_skills_source"] == "weathernext:20260925_00hr_01_preds"


def test_v2_06z_comes_from_catalog(tmp_path, fetch_mod, monkeypatch, capsys):
    _mock_catalog(monkeypatch, _catalog_like(_native_predictions(), init="2026-09-25T06:00"))
    _no_gcs(monkeypatch, fetch_mod)
    out = tmp_path / "cat06.zarr"
    argv = ["--date", "2026-09-25", "--cycle", "6", "-v", "t2m", "-o", str(out)]
    run_skill(fetch_mod.fetch, *argv)
    assert "Source: dynamical.org" in capsys.readouterr().err
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.attrs["weather_skills_source"] == "weathernext:20260925_06hr_01_preds"


@pytest.mark.parametrize(
    ("argv", "catalog_opened"),
    [
        (["-v", "t2m", "-v", "d2m"], []),  # dewpoint is GCS only
        (["-v", "t2m"], ["google-weathernext2-forecast-operational-virtual"]),  # read fails
    ],
)
def test_v2_falls_back_to_gcs(tmp_path, fetch_mod, monkeypatch, capsys, argv, catalog_opened):
    remote = _native_predictions()
    remote["2m_dewpoint_temperature"] = remote["2m_temperature"] - 5
    opened = _mock_catalog(monkeypatch, None)
    seen = []
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(
        fetch_mod, "_open_remote", lambda stamp, *a: seen.append(stamp) or remote.copy(deep=True)
    )
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", *argv, "-o", str(tmp_path / "g.zarr"))
    err = capsys.readouterr().err
    assert opened == catalog_opened
    assert "Source: gs://weathernext/" in err
    assert ("catalog read failed" in err) == bool(catalog_opened)
    assert seen and seen[0].startswith("20260925_")


def test_v2_catalog_and_gcs_write_the_same_dataset(tmp_path, fetch_mod, monkeypatch):
    native = _native_predictions(members=2)
    hours = np.array([6.0, 12.0, 18.0, 24.0], dtype="float32")
    native["2m_temperature"].values[:] = (273.15 + hours)[None, :, None, None]
    native["total_precipitation_6hr"].values[:] = (hours / 1000.0)[None, :, None, None]
    native["total_precipitation_6hr"].values[0, 0, 0, 0] = -0.0001  # clipped at zero
    argv = ["--date", "2026-09-25", "-v", "precip", "-v", "t2m", "--bbox", "3/9/0/12"]

    _mock_catalog(monkeypatch, _catalog_like(native))
    _no_gcs(monkeypatch, fetch_mod)
    run_skill(fetch_mod.fetch, *argv, "-o", str(tmp_path / "cat.zarr"))

    monkeypatch.setitem(sys.modules, "dynamical_catalog", _NoCatalog)
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *a: native.copy(deep=True))
    run_skill(fetch_mod.fetch, *argv, "-o", str(tmp_path / "gcs.zarr"))

    cat = xr.open_zarr(tmp_path / "cat.zarr", consolidated=True)
    gcs = xr.open_zarr(tmp_path / "gcs.zarr", consolidated=True)
    assert set(cat.data_vars) == set(gcs.data_vars) == {"precip", "2m_temperature"}
    assert dict(cat.sizes) == dict(gcs.sizes)
    assert set(cat.coords) == set(gcs.coords)
    for name in [*cat.data_vars, *cat.coords]:
        assert cat[name].dims == gcs[name].dims, name
        assert cat[name].dtype == gcs[name].dtype, name
        assert cat[name].attrs == gcs[name].attrs, name
    assert _lead_hours(cat) == _lead_hours(gcs) == [0, 6, 12, 18, 24]
    assert cat["precip"].attrs["units"] == "mm day-1"
    assert cat["2m_temperature"].attrs["units"] == "degree_Celsius"
    for name in ("valid_time", "time", "number", "lat", "lon"):
        np.testing.assert_array_equal(cat[name].values, gcs[name].values)
    for name in cat.data_vars:
        np.testing.assert_allclose(cat[name].values, gcs[name].values, rtol=1e-5, atol=1e-5)
    assert float(cat["precip"].min()) == 0.0
    assert cat.attrs["weather_skills_source"] == gcs.attrs["weather_skills_source"]


def test_v2_pre_2025_inits_use_the_historical_catalog(tmp_path, fetch_mod, monkeypatch):
    opened = _mock_catalog(
        monkeypatch, _catalog_like(_native_predictions(), init="2024-03-01T00:00")
    )
    _no_gcs(monkeypatch, fetch_mod)
    out = tmp_path / "hist.zarr"
    run_skill(fetch_mod.fetch, "--date", "2024-03-01", "-v", "precip", "-o", str(out))
    assert opened == ["google-weathernext2-forecast-historical-virtual"]
    with pytest.raises(SystemExit) as exc:  # dewpoint is not in the catalog, and GCS starts 2025
        run_skill(fetch_mod.fetch, "--date", "2024-03-01", "-v", "d2m", "-o", str(out))
    assert exc.value.code == 1


def test_v2_catalog_skips_an_init_still_publishing(tmp_path, fetch_mod, monkeypatch, capsys):
    native = _native_predictions()
    native["2m_temperature"].values[:, -1] = np.nan
    _mock_catalog(monkeypatch, _catalog_like(native))
    monkeypatch.setattr(fetch_mod, "_store_exists", lambda stamp, *args: True)
    monkeypatch.setattr(fetch_mod, "_open_remote", lambda *a: _native_predictions())
    run_skill(fetch_mod.fetch, "--date", "2026-09-25", "-v", "t2m", "-o", str(tmp_path / "o.zarr"))
    err = capsys.readouterr().err
    assert "20260925_00hr_01_preds is still publishing" in err
    assert "Source: gs://weathernext/" in err


@pytest.mark.parametrize("cycle", [[], ["--cycle", "18"]])
def test_v2_probe_latest_uses_catalog(capsys, fetch_mod, monkeypatch, cycle):
    import datetime as dt

    yesterday = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)).isoformat()
    native = _native_predictions()
    _mock_catalog(monkeypatch, _catalog_like(native, init=f"{yesterday}T18:00"))
    _no_gcs(monkeypatch, fetch_mod)
    run_skill(fetch_mod.fetch, "--probe-latest", *cycle)
    assert capsys.readouterr().out.strip() == yesterday


def test_v2_catalog_window_warns_on_each_missing_init(tmp_path, fetch_mod, monkeypatch, capsys):
    native = _native_predictions()
    one = _catalog_like(native, init="2025-01-30T00:00")
    cat = xr.concat(
        [one, _catalog_like(native, init="2025-02-01T00:00")],
        "init_time",
        coords="different",
        compat="equals",
    )
    _mock_catalog(monkeypatch, cat)
    _no_gcs(monkeypatch, fetch_mod)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch,
        "--start-time", "2025-01-30", "--end-time", "2025-02-01",
        "-v", "t2m", "-o", str(out),
    )  # fmt: skip
    err = capsys.readouterr().err
    assert "WARNING: dynamical.org has no init 20250131_00hr_01_preds; skipped." in err
    assert err.count("dynamical.org has no init") == 1
    with xr.open_zarr(out, consolidated=True) as ds:
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2025-01-30", "2025-02-01"]  # the missing init is not filled


# --- WeatherNext 3 statistics from the dynamical.org catalog -------------------

_CAT_15D = "google-weathernext3-forecast-15-day-0-1-degree-virtual"
_CAT_48H = "google-weathernext3-forecast-48-hour-0-1-degree-virtual"
_CAT_15D_005 = "google-weathernext3-forecast-15-day-0-05-degree-virtual"

# catalog name -> (GCS native name, catalog units, GCS units, catalog -> GCS)
_V3_FIELDS = {
    "precipitation_surface": ("total_precipitation_1hr", "kg m-2 s-1", "m", lambda v: v * 3.6),
    "temperature_2m": ("temperature_2m", "degree_Celsius", "K", lambda v: v + 273.15),
    "total_cloud_cover_atmosphere": ("total_cloud_cover", "percent", "(0 - 1)", lambda v: v / 100),
    "downward_short_wave_radiation_flux_surface": (
        "surface_solar_radiation_downwards_1hr",
        "W m-2",
        "J m**-2",
        lambda v: v * 3600,
    ),
    "wind_u_10m": ("u_component_of_wind_10m", "m s-1", "m s**-1", lambda v: v),
}


def _catalog_v3_like(init="2026-10-02T00:00", n_lead=3, stats=("mean", "p90"), seed=0):
    """The dynamical.org WeatherNext 3 statistics store: a statistic dim, y/x (float64), 1 h means."""
    rng = np.random.default_rng(seed)
    inits = np.array([init], dtype="datetime64[ns]")
    lead = (np.arange(1, n_lead + 1) * np.timedelta64(1, "h")).astype("timedelta64[ns]")
    dims = ("init_time", "statistic", "lead_time", "y", "x")
    x = np.array([0.0, 0.1, 0.2, 359.8, 359.9])
    shape = (1, len(stats), n_lead, 3, len(x))
    scale = {"kg m-2 s-1": 1e-3, "degree_Celsius": 30, "percent": 100, "W m-2": 800, "m s-1": 10}
    return xr.Dataset(
        {
            name: (
                dims,
                (rng.random(shape) * scale[units]).astype(np.float32),
                {"units": units, "step_type": "instant", "long_name": "dropped"},
            )
            for name, (_, units, _, _) in _V3_FIELDS.items()
        },
        coords={
            "init_time": inits,
            "statistic": list(stats),
            "lead_time": lead,
            "valid_time": (("init_time", "lead_time"), inits[:, None] + lead[None]),
            "expected_forecast_length": ("init_time", [np.timedelta64(360, "h")]),
            "y": [-0.1, 0.0, 0.1],
            "x": x,
            "spatial_ref": 0,
        },
    )


def _gcs_v3_like(cat):
    """The GCS statistics store holding the same values as ``cat``: <name>_<stat>, native units."""
    one = cat.isel(init_time=0)
    data_vars = {}
    for name, (gcs_name, _, units, convert) in _V3_FIELDS.items():
        for stat in one["statistic"].values:
            values = convert(one[name].sel(statistic=stat).values).astype(np.float32)
            attrs = {"units": units, "grid_degrees": 0.1, "parameter_conversion_statistic": stat}
            data_vars[f"{gcs_name}_{stat}"] = (("lead_time", "lat_0p1", "lon_0p1"), values, attrs)
    return xr.Dataset(
        data_vars,
        coords={
            "lead_time": one["lead_time"].values,
            "datetime": ("lead_time", one["valid_time"].values),
            "init_time": xr.Variable((), one["init_time"].values, {"long_name": "init"}),
            "lat_0p1": one["y"].values.astype(np.float32),
            "lon_0p1": one["x"].values.astype(np.float32),
        },
    )


def test_v3_statistics_catalog_and_gcs_write_the_same_dataset(tmp_path, fetch_mod, monkeypatch):
    cat = _catalog_v3_like(stats=("mean", "p10", "p90"))
    argv = [
        "--version", "3", "--product", "statistics", "--date", "2026-10-02",
        "-v", "precip", "-v", "t2m", "-v", "tcc", "-v", "ssrd", "-v", "u10",
        "--statistic", "mean", "--statistic", "p90", "--bbox", "0.1/-0.15/-0.1/0.15",
    ]  # fmt: skip

    _mock_catalog(monkeypatch, cat)
    _no_gcs(monkeypatch, fetch_mod)
    run_skill(fetch_mod.fetch, *argv, "-o", str(tmp_path / "cat.zarr"))

    monkeypatch.undo()  # restore GCS functions, then re-mock the store
    monkeypatch.setitem(sys.modules, "dynamical_catalog", _NoCatalog)
    _mock_v3(monkeypatch, fetch_mod, _gcs_v3_like(cat))
    run_skill(fetch_mod.fetch, *argv, "-o", str(tmp_path / "gcs.zarr"))

    a = xr.open_zarr(tmp_path / "cat.zarr", consolidated=True)
    b = xr.open_zarr(tmp_path / "gcs.zarr", consolidated=True)
    assert (
        set(a.data_vars)
        == set(b.data_vars)
        == {
            f"{name}_{stat}"
            for name in (
                "precip",
                "2m_temperature",
                "total_cloud_cover",
                "surface_solar_radiation_downwards_1hr",
                "10m_u_component_of_wind",
            )
            for stat in ("mean", "p90")
        }
    )
    assert dict(a.sizes) == dict(b.sizes) == {"step": 4, "lat": 3, "lon": 3}
    assert set(a.coords) == set(b.coords)
    assert _lead_hours(a) == _lead_hours(b) == [0, 1, 2, 3]
    for name in [*a.data_vars, *a.coords]:
        assert a[name].dims == b[name].dims, name
        assert a[name].dtype == b[name].dtype, name
        assert a[name].attrs == b[name].attrs, name
        np.testing.assert_array_equal(a[name].values, b[name].values, err_msg=name)
    assert a.attrs["weather_skills_source"] == b.attrs["weather_skills_source"]
    assert a.attrs["source"] == f"dynamical.org:{_CAT_15D}"
    assert "step_type" not in b["precip_mean"].attrs  # native attrs dropped on both paths


def test_v3_statistics_come_from_catalog_without_gcs(tmp_path, fetch_mod, monkeypatch, capsys):
    opened = _mock_catalog(monkeypatch, _catalog_v3_like())
    _no_gcs(monkeypatch, fetch_mod)
    out = tmp_path / "wn3_cat.zarr"
    run_skill(
        fetch_mod.fetch,
        "--version", "3", "--product", "statistics", "--date", "2026-10-02",
        "-v", "precip", "-v", "t2m_mean", "--statistic", "mean", "-o", str(out),
    )  # fmt: skip
    assert opened == [_CAT_15D]
    assert f"Source: dynamical.org {_CAT_15D}" in capsys.readouterr().err
    with xr.open_zarr(out, consolidated=True) as ds:
        assert set(ds.data_vars) == {"precip_mean", "2m_temperature_mean"}
        assert ds.attrs["weather_skills_source"] == "weathernext3-statistics:20261002_00hr_01_preds"


def test_v3_statistics_interim_cycle_reads_the_48_hour_store(tmp_path, fetch_mod, monkeypatch):
    opened = _mock_catalog(monkeypatch, _catalog_v3_like(init="2026-10-02T01:00"))
    _no_gcs(monkeypatch, fetch_mod)
    out = tmp_path / "x.zarr"
    run_skill(
        fetch_mod.fetch,
        "--version", "3", "--product", "statistics", "--date", "2026-10-02", "--cycle", "01",
        "-v", "t2m", "-o", str(out),
    )  # fmt: skip
    assert opened == [_CAT_48H]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.attrs["weather_skills_source"] == "weathernext3-statistics:20261002_01hr_01_preds"


def test_v3_station_head_reads_the_005_store(tmp_path, fetch_mod, monkeypatch):
    cat = _catalog_v3_like()[["temperature_2m"]]
    cat["dew_point_temperature_2m"] = cat["temperature_2m"] - 5
    opened = _mock_catalog(monkeypatch, cat)
    _no_gcs(monkeypatch, fetch_mod)
    out = tmp_path / "sh.zarr"
    run_skill(
        fetch_mod.fetch,
        "--version", "3", "--product", "statistics", "--date", "2026-10-02",
        "-v", "station_head_temperature_2m_p90", "-o", str(out),
    )  # fmt: skip
    assert opened == [_CAT_15D_005]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert list(ds.data_vars) == ["station_head_temperature_2m_p90"]
        assert ds["station_head_temperature_2m_p90"].attrs["units"] == "degree_Celsius"


@pytest.mark.parametrize(
    ("argv", "catalog", "catalog_opened", "exit_code"),
    [
        # The ensemble is GCS only.
        (["--billing-project", "p", "-v", "t2m"], True, [], None),
        # 0.1deg and 0.05deg fields in one run: no single catalog store (GCS exits 2).
        (
            ["--product", "statistics", "-v", "t2m", "-v", "station_head_temperature_2m"],
            True,
            [],
            2,
        ),
        # The catalog fails to read.
        (["--product", "statistics", "-v", "t2m"], False, [_CAT_15D], None),
    ],
)
def test_v3_falls_back_to_gcs(
    tmp_path, fetch_mod, monkeypatch, capsys, argv, catalog, catalog_opened, exit_code
):
    opened = _mock_catalog(monkeypatch, _catalog_v3_like() if catalog else None)
    remote = _native_v3_ensemble() if "--billing-project" in argv else _native_v3_statistics()
    if "station_head_temperature_2m" in argv:
        remote["station_head_temperature_2m_mean"] = xr.DataArray(
            np.zeros((3, 2, 2), np.float32), dims=("lead_time", "lat_0p05", "lon_0p05")
        )
    seen = _mock_v3(monkeypatch, fetch_mod, remote)
    args = ["--version", "3", "--date", "2026-10-02", *argv, "-o", str(tmp_path / "g.zarr")]
    if exit_code:
        with pytest.raises(SystemExit) as exc:
            run_skill(fetch_mod.fetch, *args)
        assert exc.value.code == exit_code
    else:
        run_skill(fetch_mod.fetch, *args)
    assert opened == catalog_opened
    assert seen["open"][0] == "20261002_00hr_01_preds"
    err = capsys.readouterr().err
    assert ("catalog read failed" in err) == (not catalog)
    if "statistics" in argv:
        assert "Source: gs://weathernext3_statistics_spatial/" in err


def test_v3_statistic_narrows_the_catalog_read(tmp_path, fetch_mod, monkeypatch):
    from xarray.core import indexing

    read = []

    class _Recorder(xr.backends.BackendArray):
        """A lazy array that records which statistic indices are read."""

        def __init__(self, data):
            self.data, self.shape, self.dtype = data, data.shape, data.dtype

        def __deepcopy__(self, memo):
            return self

        def __getitem__(self, key):
            return indexing.explicit_indexing_adapter(
                key, self.shape, indexing.IndexingSupport.BASIC, self._raw
            )

        def _raw(self, key):
            read.extend(np.atleast_1d(np.arange(self.shape[1])[key[1]]).tolist())
            return self.data[key]

    cat = _catalog_v3_like(stats=("mean", "p10", "p90"))
    precip = cat["precipitation_surface"]
    cat["precipitation_surface"] = xr.Variable(
        precip.dims, indexing.LazilyIndexedArray(_Recorder(precip.values)), precip.attrs
    )
    _mock_catalog(monkeypatch, cat)
    _no_gcs(monkeypatch, fetch_mod)
    run_skill(
        fetch_mod.fetch,
        "--version", "3", "--product", "statistics", "--date", "2026-10-02",
        "-v", "precip", "--statistic", "p90", "-o", str(tmp_path / "p.zarr"),
    )  # fmt: skip
    assert read and set(read) == {2}  # only p90 of mean/p10/p90

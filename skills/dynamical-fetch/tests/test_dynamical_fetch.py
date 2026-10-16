"""Correctness tests for dynamical-fetch (mocked catalog)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("dynamical-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _forecast_catalog_ds():
    inits = np.array([np.datetime64("2026-01-01")])
    steps = np.array([np.timedelta64(1, "D"), np.timedelta64(2, "D")])
    lats = [1.0, 2.0]
    lons = [10.0, 11.0]
    data = np.ones((1, 2, 2, 2))
    return xr.Dataset(
        {"tp": (("init_time", "lead_time", "latitude", "longitude"), data)},
        coords={
            "init_time": inits,
            "lead_time": steps,
            "latitude": lats,
            "longitude": lons,
        },
    )


def test_forecast_fetch_writes_zarr(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--dataset",
            "test-forecast",
            "--date",
            "2026-01-01",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    written = xr.open_zarr(out, consolidated=True)
    assert "tp" in written
    assert written["tp"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "dynamical-fetch"


def test_forecast_fetch_wraps_0_360_longitude(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    ds = ds.assign_coords(longitude=[0.0, 270.0])
    ds["tp"].values[..., 1] = 7.0
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--dataset",
            "test-forecast",
            "--date",
            "2026-01-01",
            "-o",
            str(out),
        )

    written = xr.open_zarr(out, consolidated=True)
    assert list(written["longitude"].values) == [-90.0, 0.0]
    assert float(written["tp"].isel(step=0, latitude=0).sel(longitude=-90.0)) == 7.0


def _imerg_like_analysis_ds():
    times = np.array([np.datetime64("2026-01-01T00:00"), np.datetime64("2026-01-01T00:30")])
    lats = [1.0, 2.0]
    lons = [10.0, 11.0]
    return xr.Dataset(
        {
            "precipitation_surface": (
                ("time", "latitude", "longitude"),
                np.ones((2, 2, 2)) * 1e-6,
                {"units": "kg m-2 s-1"},
            ),
            "precipitation_quality_index_surface": (
                ("time", "latitude", "longitude"),
                np.full((2, 2, 2), 0.8),
                {"units": "1"},
            ),
        },
        coords={"time": times, "latitude": lats, "longitude": lons},
    )


def test_imerg_quality_index_does_not_block_standard_units(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    state = {"ds": _imerg_like_analysis_ds(), "shape": "analysis"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--dataset",
            "nasa-imerg-analysis-late",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )

    written = xr.open_zarr(out, consolidated=True)
    assert written["precipitation_quality_index_surface"].attrs["units"] == "1"
    assert written["precipitation_surface"].attrs["units"] == "mm day-1"
    assert written["precipitation_surface"].attrs["data_interval"] == "30 minute"
    assert "aggregation_period" not in written["precipitation_surface"].attrs
    assert "aggregation_coverage" not in written.coords


def test_open_dataset_falls_back_to_staging(mod, monkeypatch):
    calls = {"list": 0, "cleared": 0}

    class _Catalog:
        @staticmethod
        def list():
            calls["list"] += 1
            if calls["list"] == 1:
                return ["nasa-imerg-analysis-late"]
            return ["nasa-imerg-analysis-late", "ucsb-chc-chirps-analysis-final"]

        @staticmethod
        def clear_cache():
            calls["cleared"] += 1

        @staticmethod
        def open(dataset):
            return _imerg_like_analysis_ds()

    monkeypatch.setitem(__import__("sys").modules, "dynamical_catalog", _Catalog)
    state = mod._open_dataset({}, "ucsb-chc-chirps-analysis-final")
    assert calls["cleared"] == 1
    assert state["shape"] == "analysis"
    assert "precipitation_surface" in state["ds"]


def test_forecast_missing_date_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        with pytest.raises(SystemExit) as exc:
            run_skill(fetch, "--dataset", "test-forecast", "-o", str(out))
    assert exc.value.code == 2


def _ifs_like_forecast_ds():
    inits = np.array([np.datetime64("2026-01-01")])
    steps = np.array([np.timedelta64(1, "D"), np.timedelta64(2, "D")])
    lats = [1.0, 2.0]
    lons = [10.0, 11.0]
    grid = np.ones((1, 2, 2, 2))
    return xr.Dataset(
        {
            "temperature_2m": (
                ("init_time", "lead_time", "latitude", "longitude"),
                grid,
                {"units": "degree_Celsius"},
            ),
            "temperature_850hpa": (
                ("init_time", "lead_time", "latitude", "longitude"),
                grid * 2,
                {"units": "degree_Celsius"},
            ),
            "temperature_925hpa": (
                ("init_time", "lead_time", "latitude", "longitude"),
                grid * 3,
                {"units": "degree_Celsius"},
            ),
            "geopotential_height_500hpa": (
                ("init_time", "lead_time", "latitude", "longitude"),
                grid * 5600,
                {"units": "m"},
            ),
        },
        coords={
            "init_time": inits,
            "lead_time": steps,
            "latitude": lats,
            "longitude": lons,
        },
    )


def test_resolve_t_alias_expands_hpa_only(mod):
    names = [
        "temperature_2m",
        "temperature_850hpa",
        "temperature_925hpa",
        "geopotential_height_500hpa",
    ]
    assert mod._resolve_variables(["t"], names, "ifs") == [
        "temperature_925hpa",
        "temperature_850hpa",
    ]
    assert mod._resolve_variables(["gh"], names, "ifs") == ["geopotential_height_500hpa"]
    assert mod._resolve_variables(["temperature_850hpa"], names, "ifs") == ["temperature_850hpa"]


def test_resolve_t_alias_uses_grouped_temperature(mod):
    names = ["average_temperature_2m", "temperature", "geopotential_height"]
    assert mod._resolve_variables(["t"], names, "er") == ["temperature"]
    assert mod._resolve_variables(["gh"], names, "er") == ["geopotential_height"]
    assert mod._resolve_variables(["t2m"], names, "er") == ["average_temperature_2m"]


def test_resolve_precip_aliases_to_surface(mod):
    names = ["precipitation_surface", "precipitation_quality_index_surface"]
    for token in ("precip", "precipitation", "tp", "total_precipitation", "pr"):
        assert mod._resolve_variables([token], names, "nasa-imerg-analysis-late") == [
            "precipitation_surface"
        ]
    assert mod._resolve_variables(["precipitation_surface"], names, "nasa-imerg-analysis-late") == [
        "precipitation_surface"
    ]


def test_resolve_unknown_lists_available(mod):
    with pytest.raises(Exception, match="Available: temperature_2m"):
        mod._resolve_variables(["tp"], ["temperature_2m"], "ifs")


def test_stack_pressure_levels_keeps_surface(mod):
    ds = xr.Dataset(
        {
            "temperature_2m": (("latitude",), [1.0, 2.0]),
            "temperature_850hpa": (("latitude",), [3.0, 4.0]),
            "temperature_925hpa": (("latitude",), [5.0, 6.0]),
        },
        coords={"latitude": [1.0, 2.0]},
    )
    out = mod._stack_pressure_levels(ds)
    assert "temperature_2m" in out
    assert "temperature" in out
    assert "temperature_850hpa" not in out
    assert list(out["vertical"].values) == [925.0, 850.0]
    assert out["vertical"].attrs["units"] == "hPa"
    assert out["temperature"].dims == ("vertical", "latitude")


def test_fetch_stacks_pressure_levels(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    state = {"ds": _ifs_like_forecast_ds(), "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--dataset",
            "ecmwf-ifs-ens-forecast-15-day-0-25-degree",
            "--date",
            "2026-01-01",
            "-v",
            "t",
            "-o",
            str(out),
        )

    written = xr.open_zarr(out, consolidated=True)
    assert "temperature" in written
    assert "temperature_2m" not in written
    assert "vertical" in written.dims
    assert list(written["vertical"].values) == [925.0, 850.0]


def test_merge_pressure_group_renames_vertical(mod):
    root = xr.Dataset(
        {
            "precipitation_surface": (
                ("init_time", "lead_time", "latitude", "longitude"),
                np.ones((1, 1, 2, 2)),
            )
        },
        coords={
            "init_time": [np.datetime64("2026-01-01")],
            "lead_time": [np.timedelta64(1, "D")],
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )
    pressure = xr.Dataset(
        {
            "temperature": (
                ("init_time", "lead_time", "pressure_level", "latitude", "longitude"),
                np.ones((1, 1, 2, 2, 2)) * 280.0,
            )
        },
        coords={
            "init_time": [np.datetime64("2026-01-01")],
            "lead_time": [np.timedelta64(1, "D")],
            "pressure_level": [1000.0, 850.0],
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )

    class _Catalog:
        @staticmethod
        def open(dataset, group=None, chunks=None):
            assert group == "pressure_level"
            return pressure

    with patch.dict("sys.modules", {"dynamical_catalog": _Catalog}):
        merged = mod._merge_pressure_if_needed(
            root, "ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree", ["t"]
        )
    assert "temperature" in merged
    assert "vertical" in merged.dims
    assert "pressure_level" not in merged.dims
    assert mod._resolve_variables(["t"], merged.data_vars, "er") == ["temperature"]


def _end_labelled_forecast_ds():
    """GEFS-like uneven leads: precip NaN at lead 0, t2m instant (finite)."""
    inits = np.array([np.datetime64("2026-01-01")])
    steps = np.array([0, 3, 6, 12], dtype="timedelta64[h]").astype("timedelta64[ns]")
    shape = (1, 4, 2, 2)
    precip = np.arange(np.prod(shape), dtype=float).reshape(shape)
    precip[:, 0] = np.nan
    return xr.Dataset(
        {
            "precipitation_surface": (
                ("init_time", "lead_time", "latitude", "longitude"),
                precip,
                {"units": "mm day-1"},
            ),
            "temperature_2m": (
                ("init_time", "lead_time", "latitude", "longitude"),
                np.ones(shape),
                {"units": "degree_Celsius"},
            ),
        },
        coords={
            "init_time": inits,
            "lead_time": steps,
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )


def _fetch_forecast(tmp_path, mod, fetch, ds, *extra):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "forecast"}):
        run_skill(fetch, "--dataset", "t", "--date", "2026-01-01", *extra, "-o", str(out))
    return xr.open_zarr(out, consolidated=True)


def test_end_labelled_lead0_dropped_and_relabelled_to_start(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip")
    hours = (written["step"].values / np.timedelta64(1, "h")).tolist()
    # Previous-step relabel: (0,3]->0, (3,6]->3, (6,12]->6 (uneven spacing kept).
    assert hours == [0, 3, 6]
    np.testing.assert_array_equal(
        written["precipitation_surface"].values,
        ds["precipitation_surface"].values[0, 1:],
    )
    assert "interval start" in written["precipitation_surface"].attrs["comment"]
    # Uneven leads carry bounds for the relabelled cells, not end-labelled ones.
    bounds = (written["step_bounds"].values / np.timedelta64(1, "h")).tolist()
    assert bounds == [[0, 3], [3, 6], [6, 12]]


def test_lead0_kept_when_any_requested_field_is_finite(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip", "-v", "temperature_2m")
    hours = (written["step"].values / np.timedelta64(1, "h")).tolist()
    assert hours == [0, 3, 6, 12]


def test_end_time_includes_whole_sub_daily_day(tmp_path, mod, fetch):
    times = np.arange(
        np.datetime64("2026-01-01T00:00"),
        np.datetime64("2026-01-03T00:30"),
        np.timedelta64(30, "m"),
    )
    ds = xr.Dataset(
        {"precipitation_surface": (("time", "latitude", "longitude"), np.ones((len(times), 1, 1)))},
        coords={"time": times, "latitude": [1.0], "longitude": [10.0]},
    )
    ds["precipitation_surface"].attrs["units"] = "mm day-1"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "analysis"}):
        run_skill(
            fetch,
            "--dataset",
            "t",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )
    written = xr.open_zarr(out, consolidated=True)
    assert written.sizes["time"] == 96
    assert str(written["time"].values[-1])[:16] == "2026-01-02T23:30"


def test_window_past_latest_time_is_reported_not_padded(tmp_path, mod, fetch, capsys):
    # Store ends 2026-01-02T05:30; asking through 2026-01-05 must warn and write
    # only the published times.
    times = np.arange(
        np.datetime64("2026-01-01T00:00"),
        np.datetime64("2026-01-02T06:00"),
        np.timedelta64(30, "m"),
    )
    ds = xr.Dataset(
        {"precipitation_surface": (("time", "latitude", "longitude"), np.ones((len(times), 1, 1)))},
        coords={"time": times, "latitude": [1.0], "longitude": [10.0]},
    )
    ds["precipitation_surface"].attrs["units"] = "mm day-1"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "analysis"}):
        run_skill(
            fetch,
            "--dataset",
            "t",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-05",
            "-o",
            str(out),
        )
    err = capsys.readouterr().err
    assert "covers only 2026-01-01T00:00..2026-01-02T05:30" in err
    assert xr.open_zarr(out, consolidated=True).sizes["time"] == len(times)


def test_complete_window_has_no_partial_warning(tmp_path, mod, fetch, capsys):
    times = np.arange(
        np.datetime64("2026-01-01"), np.datetime64("2026-01-04"), np.timedelta64(1, "D")
    )
    ds = xr.Dataset(
        {"precipitation_surface": (("time", "latitude", "longitude"), np.ones((3, 1, 1)))},
        coords={"time": times, "latitude": [1.0], "longitude": [10.0]},
    )
    ds["precipitation_surface"].attrs["units"] = "mm day-1"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "analysis"}):
        run_skill(
            fetch,
            "--dataset",
            "t",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )
    assert "covers only" not in capsys.readouterr().err


def test_still_publishing_tail_leads_dropped_and_reported(tmp_path, mod, fetch, capsys):
    ds = _end_labelled_forecast_ds()
    ds["precipitation_surface"].values[:, 3] = np.nan  # lead 12 h not published yet
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip")
    err = capsys.readouterr().err
    assert "no data after catalog lead 6 h (of 12 h" in err
    hours = (written["step"].values / np.timedelta64(1, "h")).tolist()
    assert hours == [0, 3]
    assert not bool(written["precipitation_surface"].isnull().any())


def test_init_with_no_data_exits_1(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    ds["precipitation_surface"].values[:] = np.nan
    with pytest.raises(SystemExit) as exc:
        _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip")
    assert exc.value.code == 1


def test_bbox_applied_after_time_selection(tmp_path, mod, fetch):
    # An antimeridian bbox concatenates two longitude wings, which reads every
    # value it is given: it must only ever see the requested window.
    times = np.arange(
        np.datetime64("2026-01-01"), np.datetime64("2026-03-01"), np.timedelta64(1, "D")
    )
    ds = xr.Dataset(
        {"temperature_2m": (("time", "latitude", "longitude"), np.ones((len(times), 1, 4)))},
        coords={"time": times, "latitude": [1.0], "longitude": [-175.0, -10.0, 10.0, 175.0]},
    )
    seen = []
    real = mod.bbox_subset

    def _spy(d, *a, **k):
        seen.append(d.sizes["time"])
        return real(d, *a, **k)

    out = tmp_path / "out.zarr"
    with (
        patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "analysis"}),
        patch.object(mod, "bbox_subset", _spy),
    ):
        run_skill(
            fetch,
            "--dataset",
            "t",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "5/170/-5/-170",
            "-o",
            str(out),
        )
    assert seen == [2]
    written = xr.open_zarr(out, consolidated=True)
    assert sorted(written["longitude"].values.tolist()) == [-175.0, 175.0]


def test_t2m_maps_to_instant_field_when_present(mod):
    assert mod._resolve_variables(["t2m"], ["temperature_2m"], "gfs") == ["temperature_2m"]
    assert mod._resolve_variables(["d2m"], ["dew_point_temperature_2m"], "ifs") == [
        "dew_point_temperature_2m"
    ]
    assert mod._resolve_variables(["t2m"], ["average_temperature_2m"], "er") == [
        "average_temperature_2m"
    ]


def test_resolve_surface_pressure_and_wind_aliases(mod):
    names = [
        "pressure_reduced_to_mean_sea_level",
        "pressure_surface",
        "wind_u_10m",
        "wind_v_10m",
        "wind_u_850hpa",
    ]
    got = mod._resolve_variables(["msl", "sp", "u10", "v10"], names, "ifs")
    assert got == [
        "pressure_reduced_to_mean_sea_level",
        "pressure_surface",
        "wind_u_10m",
        "wind_v_10m",
    ]
    assert mod._resolve_variables(["u"], names, "ifs") == ["wind_u_850hpa"]


def test_max_lead_keeps_leads_up_to_limit(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "temperature_2m", "--max-lead", "6h")
    hours = (written["step"].values / np.timedelta64(1, "h")).tolist()
    assert hours == [0, 3, 6]


@pytest.mark.parametrize("publishing", [False, True])
def test_probe_latest_warns_only_when_last_lead_missing(mod, fetch, capsys, publishing):
    ds = _end_labelled_forecast_ds()
    if publishing:
        ds["precipitation_surface"].values[:, -1] = np.nan
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "forecast"}):
        run_skill(fetch, "--dataset", "t", "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-01-01"
    assert ("still publishing" in captured.err) == publishing

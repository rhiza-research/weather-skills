"""Correctness tests for gfs-fetch (mocked catalog)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("gfs-fetch", "fetch")


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
            "--product",
            "gfs",
            "--date",
            "2026-01-01",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    written = xr.open_zarr(out, consolidated=True)
    assert "tp" in written
    assert written["tp"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "gfs-fetch"


def test_forecast_fetch_wraps_0_360_longitude(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    ds = ds.assign_coords(longitude=[0.0, 270.0])
    ds["tp"].values[..., 1] = 7.0
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--product",
            "gfs",
            "--date",
            "2026-01-01",
            "-o",
            str(out),
        )

    written = xr.open_zarr(out, consolidated=True)
    assert list(written["longitude"].values) == [-90.0, 0.0]
    assert float(written["tp"].isel(step=0, latitude=0).sel(longitude=-90.0)) == 7.0


def test_forecast_fetch_writes_latitude_south_to_north(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds().isel(latitude=slice(None, None, -1))
    ds["tp"].values[..., 0, :] = 5.0  # the northern row, first in the store
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(fetch, "--product", "gfs", "--date", "2026-01-01", "-o", str(out))

    written = xr.open_zarr(out, consolidated=True)
    assert list(written["latitude"].values) == [1.0, 2.0]
    assert float(written["tp"].isel(step=0).sel(latitude=2.0, longitude=10.0)) == 5.0


def test_forecast_missing_date_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    state = {"ds": ds, "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        with pytest.raises(SystemExit) as exc:
            run_skill(fetch, "--product", "gfs", "-o", str(out))
    assert exc.value.code == 2


def _gefs_like_forecast_ds():
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


def test_resolve_gh_alias_expands_hpa_only(mod):
    names = ["geopotential_height_cloud_ceiling", "geopotential_height_500hpa"]
    assert mod._resolve_variables(["gh"], names, "gefs") == ["geopotential_height_500hpa"]
    assert mod._resolve_variables(["geopotential_height"], names, "gefs") == [
        "geopotential_height_500hpa"
    ]


def test_resolve_precip_aliases_to_surface(mod):
    names = ["precipitation_surface", "precipitable_water_atmosphere"]
    for token in ("precip", "precipitation", "tp", "total_precipitation", "pr"):
        assert mod._resolve_variables([token], names, "gfs") == ["precipitation_surface"]
    assert mod._resolve_variables(["precipitation_surface"], names, "gfs") == [
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
    state = {"ds": _gefs_like_forecast_ds(), "shape": "forecast"}

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--product",
            "gfs",
            "--date",
            "2026-01-01",
            "-v",
            "gh",
            "-o",
            str(out),
        )

    written = xr.open_zarr(out, consolidated=True)
    assert "geopotential_height" in written
    assert "temperature_2m" not in written
    assert list(written["vertical"].values) == [500.0]


def _end_labelled_forecast_ds():
    """Uneven leads: precip NaN at lead 0, t2m instant (finite)."""
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
        run_skill(fetch, "--product", "gfs", "--date", "2026-01-01", *extra, "-o", str(out))
    return xr.open_zarr(out, consolidated=True)


def test_end_labelled_lead0_dropped_and_relabelled_to_start(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip")
    hours = (written["step"].values / np.timedelta64(1, "h")).tolist()
    # Previous-step relabel: (0,3]->0, (3,6]->3, (6,12]->6 (uneven spacing kept).
    assert hours == [0, 3, 6]
    np.testing.assert_array_equal(
        written["precip"].values,
        ds["precipitation_surface"].values[0, 1:],
    )
    assert "interval start" in written["precip"].attrs["comment"]
    # Uneven leads carry bounds for the relabelled cells, not end-labelled ones.
    bounds = (written["step_bounds"].values / np.timedelta64(1, "h")).tolist()
    assert bounds == [[0, 3], [3, 6], [6, 12]]


def test_instant_fields_on_uneven_leads_get_cells_from_each_step(tmp_path, mod, fetch):
    """t2m 3- then 6-hourly: each sample's cell starts at its own step."""
    leads = np.array([0, 3, 6, 12, 18], dtype="timedelta64[h]").astype("timedelta64[ns]")
    ds = xr.Dataset(
        {
            "temperature_2m": (
                ("init_time", "lead_time", "latitude", "longitude"),
                np.ones((1, 5, 2, 2)),
            )
        },
        coords={
            "init_time": [np.datetime64("2026-01-01", "ns")],
            "lead_time": leads,
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )
    ds["temperature_2m"].attrs.update(units="degree_Celsius", step_type="instant")
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "forecast"}):
        run_skill(fetch, "--product", "gfs", "--date", "2026-01-01", "-v", "t2m", "-o", str(out))
    written = xr.open_zarr(out, consolidated=True)
    assert (written["step"].values / np.timedelta64(1, "h")).tolist() == [0, 3, 6, 12, 18]
    bounds = (written["step_bounds"].values / np.timedelta64(1, "h")).tolist()
    assert bounds == [[0, 3], [3, 6], [6, 12], [12, 18], [18, 24]]


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
            "--product",
            "gfs-analysis",
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
            "--product",
            "gfs-analysis",
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
            "--product",
            "gfs-analysis",
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
    assert not bool(written["precip"].isnull().any())


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
            "--product",
            "gfs-analysis",
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


def test_resolve_surface_pressure_and_wind_aliases(mod):
    names = [
        "pressure_reduced_to_mean_sea_level",
        "pressure_surface",
        "wind_u_10m",
        "wind_v_10m",
    ]
    got = mod._resolve_variables(["msl", "sp", "u10", "v10"], names, "ifs")
    assert got == [
        "pressure_reduced_to_mean_sea_level",
        "pressure_surface",
        "wind_u_10m",
        "wind_v_10m",
    ]


def test_max_lead_keeps_leads_up_to_limit(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "temperature_2m", "--max-lead", "6h")
    hours = (written["step"].values / np.timedelta64(1, "h")).tolist()
    assert hours == [0, 3, 6]


def test_max_lead_zero_keeps_only_initial_state(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "temperature_2m", "--max-lead", "0h")
    assert (written["step"].values / np.timedelta64(1, "h")).tolist() == [0]
    np.testing.assert_array_equal(
        written["temperature_2m"].values, ds["temperature_2m"].values[0, :1]
    )


def test_max_lead_counts_output_steps_of_end_labelled_field(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip", "--max-lead", "3h")
    assert (written["step"].values / np.timedelta64(1, "h")).tolist() == [0, 3]
    np.testing.assert_array_equal(
        written["precip"].values, ds["precipitation_surface"].values[0, 1:3]
    )


def test_max_lead_zero_on_end_labelled_field_keeps_first_interval(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip", "--max-lead", "0h")
    assert (written["step"].values / np.timedelta64(1, "h")).tolist() == [0]
    np.testing.assert_array_equal(
        written["precip"].values, ds["precipitation_surface"].values[0, 1:2]
    )


def test_max_lead_zero_with_mixed_fields_drops_interval_fields(tmp_path, mod, fetch, capsys):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(
        tmp_path, mod, fetch, ds, "-v", "precip", "-v", "temperature_2m", "--max-lead", "0h"
    )
    assert list(written.data_vars) == ["temperature_2m"]
    assert (written["step"].values / np.timedelta64(1, "h")).tolist() == [0]
    assert "dropped precip" in capsys.readouterr().err


def test_max_lead_negative_exits_2(tmp_path, mod, fetch, capsys):
    ds = _end_labelled_forecast_ds()
    with pytest.raises(SystemExit) as exc:
        _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "temperature_2m", "--max-lead", "-3h")
    assert exc.value.code == 2
    assert "must not be negative" in capsys.readouterr().err


@pytest.mark.parametrize("publishing", [False, True])
def test_probe_latest_warns_only_when_last_lead_missing(mod, fetch, capsys, publishing):
    ds = _end_labelled_forecast_ds()
    if publishing:
        ds["precipitation_surface"].values[:, -1] = np.nan
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "forecast"}):
        run_skill(fetch, "--product", "gfs", "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-01-01"
    assert ("still publishing" in captured.err) == publishing


def test_list_variables_prints_fields(mod, fetch, capsys):
    root = _forecast_catalog_ds()
    root["tp"].attrs.update(units="kg m-2 s-1", long_name="Precipitation rate")
    with patch.object(mod, "_open_dataset", return_value={"ds": root, "shape": "forecast"}):
        run_skill(fetch, "--product", "gfs", "--list-variables")
    assert "  tp\tkg m-2 s-1\tPrecipitation rate" in capsys.readouterr().out


def test_unknown_variable_points_to_list_variables(mod):
    with pytest.raises(Exception) as exc:
        mod._resolve_variables(["list_vars_please"], ["tp"], "x")
    assert "--list-variables" in str(exc.value)


def _daily_inits_forecast_ds():
    """Daily 00 UTC inits (plus one 12 UTC), daily leads 1..3 D; value = init day of month."""
    inits = np.array(
        ["2025-01-30", "2025-01-31", "2025-02-01", "2025-02-01T12", "2026-01-15", "2026-02-10"],
        dtype="datetime64[ns]",
    )
    steps = np.array([1, 2, 3], dtype="timedelta64[D]").astype("timedelta64[ns]")
    days = inits.astype("datetime64[D]").astype(object)
    data = np.array([d.day for d in days], dtype=float)[:, None, None, None] * np.ones((1, 3, 2, 2))
    return xr.Dataset(
        {"temperature_2m": (("init_time", "lead_time", "latitude", "longitude"), data)},
        coords={
            "init_time": inits,
            "lead_time": steps,
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )


def _fetch_windows(tmp_path, mod, fetch, ds, *extra):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "forecast"}):
        run_skill(fetch, "--product", "gfs", *extra, "-o", str(out))
    return xr.open_zarr(out, consolidated=True)


def test_init_range_keeps_00utc_inits_on_a_time_axis(tmp_path, mod, fetch):
    written = _fetch_windows(
        tmp_path,
        mod,
        fetch,
        _daily_inits_forecast_ds(),
        "--start-time",
        "2025-01-31",
        "--end-time",
        "2025-02-01",
    )
    assert written["temperature_2m"].dims[:2] == ("time", "step")
    # 2025-02-01T12 is not a 00 UTC init.
    assert written["time"].values.astype("datetime64[D]").astype(str).tolist() == [
        "2025-01-31",
        "2025-02-01",
    ]
    assert load_history(tmp_path / "out.zarr")[-1]["args"]["start_time"] == ["2025-01-31"]


def test_init_range_stamps_lead_spacing_not_init_spacing(tmp_path, mod, fetch):
    """Daily inits with 6 h leads: data_interval is the 6 h step, not the 1-day init gap."""
    ds = _daily_inits_forecast_ds().assign_coords(
        lead_time=np.array([6, 12, 18], dtype="timedelta64[h]").astype("timedelta64[ns]")
    )
    written = _fetch_windows(
        tmp_path, mod, fetch, ds, "--start-time", "2025-01-30", "--end-time", "2025-02-01"
    )
    assert written.sizes["time"] == 3
    assert written["temperature_2m"].attrs["data_interval"] == "6 hour"


def test_repeated_windows_take_disjoint_inits(tmp_path, mod, fetch):
    written = _fetch_windows(
        tmp_path,
        mod,
        fetch,
        _daily_inits_forecast_ds(),
        "--start-time",
        "2025-01-01",
        "--end-time",
        "2025-01-31",
        "--start-time",
        "2026-01-01",
        "--end-time",
        "2026-01-31",
    )
    days = written["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2025-01-30", "2025-01-31", "2026-01-15"]


def test_init_range_reports_still_publishing_newest_init(tmp_path, mod, fetch, capsys):
    ds = _daily_inits_forecast_ds().isel(init_time=[0, 1])
    ds["temperature_2m"][1, 2] = np.nan
    written = _fetch_windows(
        tmp_path, mod, fetch, ds, "--start-time", "2025-01-30", "--end-time", "2025-01-31"
    )
    err = capsys.readouterr().err
    assert "2025-01-31T00 UTC init has no data after catalog lead 48 h" in err
    # Its last lead stays NaN; the older init keeps every lead.
    assert written["temperature_2m"].isel(time=1, step=2).isnull().all()
    assert written["temperature_2m"].isel(time=0).notnull().all()


def test_forecast_date_and_range_together_exit_2(tmp_path, mod, fetch):
    with pytest.raises(SystemExit) as exc:
        _fetch_windows(
            tmp_path,
            mod,
            fetch,
            _daily_inits_forecast_ds(),
            "--date",
            "2025-01-31",
            "--start-time",
            "2025-01-31",
            "--end-time",
            "2025-02-01",
        )
    assert exc.value.code == 2


def test_forecast_range_with_no_init_is_data_error(tmp_path, mod, fetch):
    with pytest.raises(SystemExit) as exc:
        _fetch_windows(
            tmp_path,
            mod,
            fetch,
            _daily_inits_forecast_ds(),
            "--start-time",
            "2024-06-01",
            "--end-time",
            "2024-06-30",
        )
    assert exc.value.code == 1


def test_analysis_repeated_windows_take_each_window(tmp_path, mod, fetch):
    times = np.arange(
        np.datetime64("2024-12-30"), np.datetime64("2026-01-03"), np.timedelta64(1, "D")
    ).astype("datetime64[ns]")
    ds = xr.Dataset(
        {
            "precipitation_surface": (
                ("time", "latitude", "longitude"),
                np.ones((times.size, 2, 2)),
                {"units": "mm day-1"},
            )
        },
        coords={"time": times, "latitude": [1.0, 2.0], "longitude": [10.0, 11.0]},
    )
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "analysis"}):
        run_skill(
            fetch,
            "--product",
            "gfs-analysis",
            "--start-time",
            "2025-01-01",
            "--end-time",
            "2025-01-02",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-05",
            "-o",
            str(out),
        )
    days = xr.open_zarr(out)["time"].values.astype("datetime64[D]").astype(str).tolist()
    # The second window runs past the data: kept as far as it goes, not padded.
    assert days == ["2025-01-01", "2025-01-02", "2026-01-01", "2026-01-02"]


@pytest.mark.parametrize(
    ("extra", "dims"),
    [
        (("--date", "2026-01-01"), ("step", "number", "latitude", "longitude")),
        (
            ("--start-time", "2026-01-01", "--end-time", "2026-01-02"),
            ("time", "step", "number", "latitude", "longitude"),
        ),
    ],
)
def test_members_stored_before_leads_come_out_after_step(tmp_path, mod, fetch, extra, dims):
    # GEFS stores ensemble_member before lead_time.
    data = np.ones((2, 2, 2, 2, 2))
    ds = xr.Dataset(
        {"tp": (("init_time", "ensemble_member", "lead_time", "latitude", "longitude"), data)},
        coords={
            "init_time": np.array(["2026-01-01", "2026-01-02"], dtype="datetime64[ns]"),
            "ensemble_member": [0, 1],
            "lead_time": np.array([1, 2], dtype="timedelta64[D]").astype("timedelta64[ns]"),
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "ensemble"}):
        run_skill(fetch, "--product", "gefs", *extra, "-o", str(out))
    assert xr.open_zarr(out, consolidated=True)["tp"].dims == dims


@pytest.mark.parametrize(
    ("product", "dataset"),
    [
        ("gfs", "noaa-gfs-forecast"),
        ("gefs", "noaa-gefs-forecast-35-day"),
        ("gfs-analysis", "noaa-gfs-analysis"),
        ("gefs-analysis", "noaa-gefs-analysis"),
    ],
)
def test_product_picks_catalog_id(tmp_path, mod, fetch, product, dataset):
    ds = _forecast_catalog_ds()
    shape = "forecast"
    when = ["--date", "2026-01-01"]
    if product.endswith("analysis"):
        ds = ds.isel(init_time=0, lead_time=0, drop=True).expand_dims(
            time=[np.datetime64("2026-01-01", "ns")]
        )
        shape = "analysis"
        when = ["--start-time", "2026-01-01", "--end-time", "2026-01-01"]
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": shape}) as opened:
        run_skill(fetch, "--product", product, *when, "-o", str(out))
    opened.assert_called_once_with(dataset)
    assert xr.open_zarr(out).attrs["weather_skills_source"] == f"dynamical:{dataset}"


def test_default_product_is_gefs(mod, fetch, capsys):
    ds = _end_labelled_forecast_ds()
    with patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "forecast"}) as opened:
        run_skill(fetch, "--probe-latest")
    opened.assert_called_once_with("noaa-gefs-forecast-35-day")


def test_precip_written_as_precip(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    ds["precipitation_surface"].attrs["units"] = "kg m-2 s-1"
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "tp")
    assert list(written.data_vars) == ["precip"]
    assert written["precip"].attrs["units"] == "mm day-1"
    # 1 kg m-2 s-1 is 86400 mm day-1.
    np.testing.assert_allclose(
        written["precip"].values, ds["precipitation_surface"].values[0, 1:] * 86400
    )


HINDCAST = "gs://sheerwater-public-datalake/hindcasts"


def _hindcast_store(name, inits, value):
    """One-variable GEFSv12 reforecast store as Sheerwater lays it out."""
    shape = (len(inits), 2, 3, 3, 3)
    return xr.Dataset(
        {
            name: (
                ("init_time", "member", "prediction_timedelta", "lat", "lon"),
                np.full(shape, value),
            )
        },
        coords={
            "init_time": np.array(inits, dtype="datetime64[ns]"),
            "member": np.array([0, 1], dtype=np.int32),
            "prediction_timedelta": np.arange(3).astype("timedelta64[D]").astype("timedelta64[ns]"),
            "lat": np.array([-1.0, 0.0, 1.0], dtype=np.float32),
            "lon": np.array([-1.0, 0.0, 1.0], dtype=np.float32),
        },
    )


@pytest.fixture
def hindcast(mod):
    stores = {
        f"{HINDCAST}/gefs_precip.zarr": _hindcast_store(
            "precip", ["2010-03-03", "2010-03-10"], 2.0
        ),
        # The tmp2m store skips 2010-03-03.
        f"{HINDCAST}/gefs_tmp2m.zarr": _hindcast_store("tmp2m", ["2010-03-10"], 25.0),
    }
    with (
        patch.object(mod, "_open_hindcast", side_effect=stores.__getitem__) as opened,
        patch.object(mod, "_open_dataset", side_effect=AssertionError("catalog opened")),
    ):
        yield opened


def test_live_gefs_init_reads_catalog_not_hindcast(tmp_path, mod, fetch):
    ds = _forecast_catalog_ds().assign_coords(init_time=[np.datetime64("2024-01-03", "ns")])
    with (
        patch.object(mod, "_open_hindcast", side_effect=AssertionError("hindcast opened")),
        patch.object(mod, "_open_dataset", return_value={"ds": ds, "shape": "forecast"}) as opened,
    ):
        run_skill(
            fetch, "--product", "gefs", "--date", "2024-01-03", "-o", str(tmp_path / "o.zarr")
        )
    opened.assert_called_once_with("noaa-gefs-forecast-35-day")


def test_historical_gefs_window_reads_hindcast_like_live(tmp_path, fetch, hindcast, capsys):
    out = tmp_path / "h.zarr"
    run_skill(fetch, "--product", "gefs", "--start-time", "2010-03-01", "--end-time", "2010-03-31",
              "--bbox", "2/-2/-2/2", "-v", "precip", "-v", "t2m", "--max-lead", "1D",
              "-o", str(out))  # fmt: skip
    err = capsys.readouterr().err
    assert "sheerwater-hindcast:gefs" in err
    # A store without an init stays NaN for it, with a warning; never filled.
    assert "temperature_2m has no run for 1 init(s), left NaN (not filled): 2010-03-03" in err
    ds = xr.open_zarr(out)
    assert sorted(ds.data_vars) == ["precip", "temperature_2m"]
    for name in ds.data_vars:
        assert ds[name].dims == ("time", "step", "number", "latitude", "longitude")
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["temperature_2m"].attrs["units"] == "degree_Celsius"
    assert ds["precip"].attrs["data_interval"] == "1 day"
    assert list(ds["number"].values) == [0, 1]
    assert list(ds["step"].values) == [np.timedelta64(0, "ns"), np.timedelta64(1, "D")]
    assert ds.attrs["weather_skills_source"] == "sheerwater-hindcast:gefs"
    t2m = ds["temperature_2m"].isel(step=0, number=0, latitude=0, longitude=0).values
    assert np.isnan(t2m[0]) and t2m[1] == 25.0
    assert load_history(out)[-1]["skill"] == "gfs-fetch"


def test_historical_gefs_date_has_scalar_time(tmp_path, fetch, hindcast):
    out = tmp_path / "h.zarr"
    run_skill(fetch, "--product", "gefs", "--date", "2010-03-10", "-o", str(out))
    ds = xr.open_zarr(out)
    assert ds["precip"].dims == ("step", "number", "latitude", "longitude")
    assert ds["time"].values == np.datetime64("2010-03-10", "ns")


def test_gefs_window_across_live_start_exits_2(tmp_path, fetch, hindcast, capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--product", "gefs", "--start-time", "2019-01-01",
                  "--end-time", "2021-01-01", "-o", str(tmp_path / "o.zarr"))  # fmt: skip
    assert exc.value.code == 2
    assert "split the request there" in capsys.readouterr().err
    hindcast.assert_not_called()


def test_historical_gefs_no_wednesday_exits_1(tmp_path, fetch, hindcast, capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--product", "gefs", "--start-time", "2010-03-04",
                  "--end-time", "2010-03-09", "-o", str(tmp_path / "o.zarr"))  # fmt: skip
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Wednesday" in err and "2010-03-03..2010-03-10" in err


def test_historical_gefs_unknown_variable_exits_2(tmp_path, fetch, hindcast, capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--product", "gefs", "--date", "2010-03-10", "-v", "u10",
                  "-o", str(tmp_path / "o.zarr"))  # fmt: skip
    assert exc.value.code == 2
    assert "offer only precip" in capsys.readouterr().err

"""Correctness tests for icon-eu-fetch (mocked catalog)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("icon-eu-fetch", "fetch")


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
    state = ds

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
            "--date",
            "2026-01-01",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    written = xr.open_zarr(out, consolidated=True)
    assert "tp" in written
    assert written["tp"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "icon-eu-fetch"


def test_forecast_fetch_wraps_0_360_longitude(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    ds = ds.assign_coords(longitude=[0.0, 270.0])
    ds["tp"].values[..., 1] = 7.0
    state = ds

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(
            fetch,
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
    state = ds

    with patch.object(mod, "_open_dataset", return_value=state):
        run_skill(fetch, "--date", "2026-01-01", "-o", str(out))

    written = xr.open_zarr(out, consolidated=True)
    assert list(written["latitude"].values) == [1.0, 2.0]
    assert float(written["tp"].isel(step=0).sel(latitude=2.0, longitude=10.0)) == 5.0


def test_forecast_missing_date_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    ds = _forecast_catalog_ds()
    state = ds

    with patch.object(mod, "_open_dataset", return_value=state):
        with pytest.raises(SystemExit) as exc:
            run_skill(fetch, "-o", str(out))
    assert exc.value.code == 2


def test_resolve_precip_aliases_to_surface(mod):
    names = ["precipitation_surface", "precipitable_water_atmosphere"]
    for token in ("precip", "precipitation", "tp", "total_precipitation", "pr"):
        assert mod._resolve_variables([token], names) == ["precipitation_surface"]
    assert mod._resolve_variables(["precipitation_surface"], names) == ["precipitation_surface"]


def test_resolve_unknown_lists_available(mod):
    with pytest.raises(Exception, match="Available: temperature_2m"):
        mod._resolve_variables(["tp"], ["temperature_2m"])


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
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(fetch, "--date", "2026-01-01", *extra, "-o", str(out))
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
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(fetch, "--date", "2026-01-01", "-v", "t2m", "-o", str(out))
    written = xr.open_zarr(out, consolidated=True)
    assert (written["step"].values / np.timedelta64(1, "h")).tolist() == [0, 3, 6, 12, 18]
    bounds = (written["step_bounds"].values / np.timedelta64(1, "h")).tolist()
    assert bounds == [[0, 3], [3, 6], [6, 12], [12, 18], [18, 24]]


def test_lead0_kept_when_any_requested_field_is_finite(tmp_path, mod, fetch):
    ds = _end_labelled_forecast_ds()
    written = _fetch_forecast(tmp_path, mod, fetch, ds, "-v", "precip", "-v", "temperature_2m")
    hours = (written["step"].values / np.timedelta64(1, "h")).tolist()
    assert hours == [0, 3, 6, 12]


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


def test_t2m_maps_to_instant_field_when_present(mod):
    assert mod._resolve_variables(["t2m"], ["temperature_2m"]) == ["temperature_2m"]
    assert mod._resolve_variables(["d2m"], ["dew_point_temperature_2m"]) == [
        "dew_point_temperature_2m"
    ]


def test_resolve_surface_pressure_and_wind_aliases(mod):
    names = [
        "pressure_reduced_to_mean_sea_level",
        "pressure_surface",
        "wind_u_10m",
        "wind_v_10m",
    ]
    got = mod._resolve_variables(["msl", "sp", "u10", "v10"], names)
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
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(fetch, "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-01-01"
    assert ("still publishing" in captured.err) == publishing


def test_list_variables_prints_fields(mod, fetch, capsys):
    root = _forecast_catalog_ds()
    root["tp"].attrs.update(units="kg m-2 s-1", long_name="Precipitation rate")
    with patch.object(mod, "_open_dataset", return_value=root):
        run_skill(fetch, "--list-variables")
    assert "  tp\tkg m-2 s-1\tPrecipitation rate" in capsys.readouterr().out


def test_unknown_variable_points_to_list_variables(mod):
    with pytest.raises(Exception) as exc:
        mod._resolve_variables(["list_vars_please"], ["tp"])
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
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(fetch, *extra, "-o", str(out))
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

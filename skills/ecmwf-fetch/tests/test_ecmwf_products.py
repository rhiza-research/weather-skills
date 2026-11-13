"""Correctness tests for the dynamical.org products of ecmwf-fetch (mocked catalog)."""

from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import DataError
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("ecmwf-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _catalog(root, pressure=None):
    """Stand-in for `_open_catalog`: the root store, and the pressure group if given."""

    def open_catalog(dataset, group=None):
        if group is None:
            return root
        if pressure is None:
            raise DataError(f"{dataset} has no group {group}")
        return pressure

    return open_catalog


def _end_labelled_ds():
    """IFS-ENS-like uneven leads: precip NaN at lead 0, t2m instant (finite)."""
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
            "init_time": np.array(["2026-01-01"], dtype="datetime64[ns]"),
            "lead_time": steps,
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )


def _run(tmp_path, mod, fetch, root, *extra, product="aifs-single", pressure=None):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_catalog", side_effect=_catalog(root, pressure)):
        run_skill(fetch, "--product", product, *extra, "-o", str(out))
    return xr.open_zarr(out, consolidated=True)


def _fetch(tmp_path, mod, fetch, root, *extra, **kwargs):
    return _run(tmp_path, mod, fetch, root, "--date", "2026-01-01", *extra, **kwargs)


def _hours(ds):
    return (ds["step"].values / np.timedelta64(1, "h")).tolist()


def test_short_names_and_source(tmp_path, mod, fetch):
    written = _fetch(tmp_path, mod, fetch, _end_labelled_ds(), "-v", "precip", "-v", "t2m")
    assert sorted(written.data_vars) == ["precip", "t2m"]
    assert written.attrs["weather_skills_source"] == "dynamical:ecmwf-aifs-single-forecast"
    assert written["t2m"].attrs["units"] == "degree_Celsius"
    assert written["time"].ndim == 0
    assert load_history(tmp_path / "out.zarr")[-1]["skill"] == "ecmwf-fetch"


def test_unmapped_catalog_name_is_kept(tmp_path, mod, fetch):
    root = _end_labelled_ds().rename(temperature_2m="categorical_precipitation_type_surface")
    root["categorical_precipitation_type_surface"].attrs["units"] = "W m-2"
    written = _fetch(tmp_path, mod, fetch, root, "-v", "categorical_precipitation_type_surface")
    assert list(written.data_vars) == ["categorical_precipitation_type_surface"]
    assert written["categorical_precipitation_type_surface"].attrs["units"] == "W m-2"


def test_end_labelled_lead0_dropped_and_relabelled_to_start(tmp_path, mod, fetch):
    ds = _end_labelled_ds()
    written = _fetch(tmp_path, mod, fetch, ds, "-v", "precip", product="ifs-ens")
    # Previous-step relabel: (0,3]->0, (3,6]->3, (6,12]->6 (uneven spacing kept).
    assert _hours(written) == [0, 3, 6]
    np.testing.assert_array_equal(
        written["precip"].values, ds["precipitation_surface"].values[0, 1:]
    )
    assert "interval start" in written["precip"].attrs["comment"]
    bounds = (written["step_bounds"].values / np.timedelta64(1, "h")).tolist()
    assert bounds == [[0, 3], [3, 6], [6, 12]]


def test_instant_fields_on_uneven_leads_get_cells_from_each_step(tmp_path, mod, fetch):
    ds = _end_labelled_ds()[["temperature_2m"]]
    written = _fetch(tmp_path, mod, fetch, ds, "-v", "t2m", product="ifs-ens")
    assert _hours(written) == [0, 3, 6, 12]
    bounds = (written["step_bounds"].values / np.timedelta64(1, "h")).tolist()
    assert bounds == [[0, 3], [3, 6], [6, 12], [12, 18]]


def test_lead0_kept_when_any_requested_field_is_finite(tmp_path, mod, fetch):
    written = _fetch(tmp_path, mod, fetch, _end_labelled_ds(), "-v", "precip", "t2m")
    assert _hours(written) == [0, 3, 6, 12]


def test_still_publishing_tail_leads_dropped_and_reported(tmp_path, mod, fetch, capsys):
    ds = _end_labelled_ds()
    ds["precipitation_surface"].values[:, 3] = np.nan
    written = _fetch(tmp_path, mod, fetch, ds, "-v", "precip")
    assert "no data after catalog lead 6 h (of 12 h" in capsys.readouterr().err
    assert _hours(written) == [0, 3]
    assert not bool(written["precip"].isnull().any())


def test_init_with_no_data_exits_1(tmp_path, mod, fetch):
    ds = _end_labelled_ds()
    ds["precipitation_surface"].values[:] = np.nan
    with pytest.raises(SystemExit) as exc:
        _fetch(tmp_path, mod, fetch, ds, "-v", "precip")
    assert exc.value.code == 1


def _hpa_ds():
    grid = np.ones((1, 2, 2, 2))
    dims = ("init_time", "lead_time", "latitude", "longitude")
    return xr.Dataset(
        {
            "temperature_2m": (dims, grid, {"units": "degree_Celsius"}),
            "temperature_850hpa": (dims, grid * 2, {"units": "degree_Celsius"}),
            "temperature_925hpa": (dims, grid * 3, {"units": "degree_Celsius"}),
            "geopotential_height_500hpa": (dims, grid * 5600, {"units": "m"}),
            "wind_u_10m": (dims, grid, {"units": "m s-1"}),
        },
        coords={
            "init_time": np.array(["2026-01-01"], dtype="datetime64[ns]"),
            "lead_time": np.array([1, 2], dtype="timedelta64[D]").astype("timedelta64[ns]"),
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )


def test_resolve_level_alias_expands_hpa_fields_only(mod):
    names = list(_hpa_ds().data_vars)
    assert mod._resolve_catalog(["t"], names, "ifs") == ["temperature_925hpa", "temperature_850hpa"]
    assert mod._resolve_catalog(["gh"], names, "ifs") == ["geopotential_height_500hpa"]
    assert mod._resolve_catalog(["t2m"], names, "ifs") == ["temperature_2m"]
    assert mod._resolve_catalog(["u10"], names, "ifs") == ["wind_u_10m"]
    with pytest.raises(Exception, match="--list-variables"):
        mod._resolve_catalog(["u"], names, "ifs")


def test_pressure_levels_stack_onto_vertical(tmp_path, mod, fetch):
    written = _fetch(tmp_path, mod, fetch, _hpa_ds(), "-v", "t", product="ifs-ens")
    assert list(written.data_vars) == ["t"]
    assert written["t"].dims[0] == "vertical"
    assert written["vertical"].values.tolist() == [925.0, 850.0]
    assert written["vertical"].attrs["units"] == "hPa"


def _s2s_ds(name, units, dims=(), coords=None, n_lead=4, lead0_nan=True):
    full = ("init_time", "lead_time", "ensemble_member", *dims, "latitude", "longitude")
    lons = [-180.0, -90.0, 0.0, 90.0]
    shape = (1, n_lead, 2, *[len(v) for v in (coords or {}).values()], 2, len(lons))
    data = np.full(shape, 230.0)
    if lead0_nan:
        data[:, 0] = np.nan
    return xr.Dataset(
        {name: (full, data, {"units": units})},
        coords={
            "init_time": np.array(["2026-03-02"], dtype="datetime64[ns]"),
            "lead_time": np.arange(n_lead) * np.timedelta64(1, "D"),
            "ensemble_member": [0, 1],
            **(coords or {}),
            "latitude": [10.0, -10.0],
            "longitude": lons,
        },
    )


def test_s2s_olr_by_catalog_name_is_ttr_w_m2_daily_from_step_0(tmp_path, mod, fetch):
    olr = "net_long_wave_radiation_flux_top_of_atmosphere"
    root = _s2s_ds(olr, "W m-2")
    written = _run(
        tmp_path,
        mod,
        fetch,
        root,
        "--date",
        "2026-03-02",
        "--bbox",
        "30/-180/-30/180",
        "-v",
        olr,
        product="s2s",
    )
    assert list(written.data_vars) == ["ttr"]
    assert written["ttr"].attrs["units"] == "W m-2"
    assert written["time"].ndim == 0
    assert np.asarray(written["step"].values).astype("timedelta64[D]").astype(int).tolist() == [
        0,
        1,
        2,
    ]
    assert written["ttr"].attrs["data_interval"] == "1 day"
    assert written.sizes["longitude"] == 4
    assert written.attrs["weather_skills_source"] == "ecmwf-s2s"


def test_s2s_pressure_group_u_by_short_name(tmp_path, mod, fetch):
    root = _s2s_ds("precipitation_surface", "kg m-2 s-1")
    pressure = _s2s_ds(
        "wind_u", "m s-1", ("pressure_level",), {"pressure_level": [850.0, 200.0]}, lead0_nan=False
    )
    written = _run(
        tmp_path,
        mod,
        fetch,
        root,
        "--date",
        "2026-03-02",
        "--bbox",
        "30/-180/-30/180",
        "-v",
        "u",
        product="s2s",
        pressure=pressure,
    )
    assert list(written.data_vars) == ["u"]
    assert written["vertical"].values.tolist() == [850.0, 200.0]
    assert written.sizes["step"] == 4  # instant field keeps lead 0


def test_max_lead_keeps_leads_up_to_limit(tmp_path, mod, fetch):
    written = _fetch(tmp_path, mod, fetch, _end_labelled_ds(), "-v", "t2m", "--max-lead", "6h")
    assert _hours(written) == [0, 3, 6]


def test_max_lead_zero_keeps_only_initial_state(tmp_path, mod, fetch):
    written = _fetch(tmp_path, mod, fetch, _end_labelled_ds(), "-v", "t2m", "--max-lead", "0")
    assert _hours(written) == [0]


def test_max_lead_counts_output_steps_of_end_labelled_field(tmp_path, mod, fetch):
    ds = _end_labelled_ds()
    written = _fetch(tmp_path, mod, fetch, ds, "-v", "precip", "--max-lead", "3h")
    assert _hours(written) == [0, 3]
    np.testing.assert_array_equal(
        written["precip"].values, ds["precipitation_surface"].values[0, 1:3]
    )


def test_max_lead_zero_with_mixed_fields_drops_interval_fields(tmp_path, mod, fetch, capsys):
    written = _fetch(
        tmp_path, mod, fetch, _end_labelled_ds(), "-v", "precip", "t2m", "--max-lead", "0h"
    )
    assert list(written.data_vars) == ["t2m"]
    assert "dropped precipitation_surface" in capsys.readouterr().err


def test_max_lead_negative_exits_2(tmp_path, mod, fetch, capsys):
    with pytest.raises(SystemExit) as exc:
        _fetch(tmp_path, mod, fetch, _end_labelled_ds(), "-v", "t2m", "--max-lead", "-3h")
    assert exc.value.code == 2
    assert "must not be negative" in capsys.readouterr().err


def _daily_inits_ds():
    """Daily 00 UTC inits (plus one 12 UTC), 2 members; value = init day of month."""
    inits = np.array(
        ["2025-01-30", "2025-01-31", "2025-02-01", "2025-02-01T12", "2026-01-15"],
        dtype="datetime64[ns]",
    )
    days = np.array([d.day for d in inits.astype("datetime64[D]").astype(object)], dtype=float)
    data = days[:, None, None, None, None] * np.ones((1, 3, 2, 2, 2))
    return xr.Dataset(
        {
            "temperature_2m": (
                ("init_time", "lead_time", "ensemble_member", "latitude", "longitude"),
                data,
                {"units": "degree_Celsius"},
            )
        },
        coords={
            "init_time": inits,
            "lead_time": np.array([6, 12, 18], dtype="timedelta64[h]").astype("timedelta64[ns]"),
            "ensemble_member": [0, 1],
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )


def _windows(tmp_path, mod, fetch, ds, *extra):
    return _run(tmp_path, mod, fetch, ds, "-v", "t2m", *extra, product="aifs-ens")


def test_init_window_keeps_00utc_inits_on_a_time_axis(tmp_path, mod, fetch):
    written = _windows(
        tmp_path,
        mod,
        fetch,
        _daily_inits_ds(),
        "--start-time",
        "2025-01-31",
        "--end-time",
        "2025-02-01",
    )
    assert written["t2m"].dims == ("time", "number", "step", "latitude", "longitude")
    assert written["time"].values.astype("datetime64[D]").astype(str).tolist() == [
        "2025-01-31",
        "2025-02-01",
    ]
    # data_interval is the 6 h lead spacing, not the 1-day init gap.
    assert written["t2m"].attrs["data_interval"] == "6 hour"


def test_repeated_windows_take_disjoint_inits(tmp_path, mod, fetch):
    written = _windows(
        tmp_path,
        mod,
        fetch,
        _daily_inits_ds(),
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


def test_init_window_reports_still_publishing_newest_init(tmp_path, mod, fetch, capsys):
    ds = _daily_inits_ds().isel(init_time=[0, 1])
    ds["temperature_2m"][1, 2] = np.nan
    written = _windows(
        tmp_path, mod, fetch, ds, "--start-time", "2025-01-30", "--end-time", "2025-01-31"
    )
    assert "2025-01-31T00 UTC init has no data after catalog lead 12 h" in capsys.readouterr().err
    assert written["t2m"].isel(time=1, step=2).isnull().all()
    assert written["t2m"].isel(time=0).notnull().all()


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        (("--date", "2025-01-31", "--start-time", "2025-01-31", "--end-time", "2025-02-01"), 2),
        (("--start-time", "2025-01-31"), 2),
        ((), 2),
        (("--start-time", "2024-06-01", "--end-time", "2024-06-30"), 1),
    ],
)
def test_init_selection_errors(tmp_path, mod, fetch, extra, code):
    with pytest.raises(SystemExit) as exc:
        _windows(tmp_path, mod, fetch, _daily_inits_ds(), *extra)
    assert exc.value.code == code


@pytest.mark.parametrize("publishing", [False, True])
def test_probe_latest_warns_only_when_last_lead_missing(mod, fetch, capsys, publishing):
    ds = _end_labelled_ds()
    if publishing:
        ds["precipitation_surface"].values[:, -1] = np.nan
    with patch.object(mod, "_open_catalog", side_effect=_catalog(ds)):
        run_skill(fetch, "--product", "ifs-ens", "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-01-01"
    assert ("still publishing" in captured.err) == publishing


def test_list_variables_prints_root_and_pressure_level_fields(mod, fetch, capsys):
    root = _end_labelled_ds()
    root["precipitation_surface"].attrs.update(long_name="Precipitation rate")
    pressure = xr.Dataset(
        {"specific_humidity": (("pressure_level",), [0.01, 0.005], {"units": "1"})},
        coords={"pressure_level": [1000.0, 700.0]},
    )
    with patch.object(mod, "_open_catalog", side_effect=_catalog(root, pressure)):
        run_skill(fetch, "--list-variables")
    out = capsys.readouterr().out
    assert "  precipitation_surface\tmm day-1\tPrecipitation rate" in out
    assert "levels 1000, 700 hPa" in out
    assert "  specific_humidity\t1\t" in out
    assert "ECMWF Data Store fields" in out and "pv" in out


def test_list_variables_without_pressure_group(mod, fetch, capsys):
    with patch.object(mod, "_open_catalog", side_effect=_catalog(_end_labelled_ds())):
        run_skill(fetch, "--product", "aifs-ens", "--list-variables")
    out = capsys.readouterr().out
    assert "  temperature_2m\t" in out
    assert "pressure_level group" not in out and "Data Store" not in out

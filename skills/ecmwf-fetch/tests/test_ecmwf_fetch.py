"""Correctness tests for ecmwf-fetch (no network; helpers + missing creds)."""

import os
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import UsageError
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("ecmwf-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_missing_ecmwf_env_exits_2(tmp_path, fetch):
    """Unmapped fields still require ECDS credentials."""
    out = tmp_path / "out.zarr"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")
    }

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--date",
                "2026-01-01",
                "--bbox",
                "3/10/0/13",
                "-v",
                "pv",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_ensure_datastores_url_sets_hardcoded_default(mod):
    """The ECDS store URL is not a secret — it's set even if absent from env."""
    env = {k: v for k, v in os.environ.items() if k != "ECMWF_DATASTORES_URL"}
    with patch.dict(os.environ, env, clear=True):
        mod._ensure_datastores_url()
        assert os.environ["ECMWF_DATASTORES_URL"] == "https://ecds.ecmwf.int/api"


def test_ensure_datastores_url_does_not_override_explicit_env(mod):
    """An operator-provided override (e.g. staging) still wins."""
    env = {**os.environ, "ECMWF_DATASTORES_URL": "https://example.invalid"}
    with patch.dict(os.environ, env, clear=True):
        mod._ensure_datastores_url()
        assert os.environ["ECMWF_DATASTORES_URL"] == "https://example.invalid"


def test_unknown_variable_exits_2(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    env = {
        **os.environ,
        "ECMWF_DATASTORES_URL": "https://example.invalid",
        "ECMWF_DATASTORES_KEY": "dummy",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--date",
                "2026-01-01",
                "--bbox",
                "3/10/0/13",
                "-v",
                "2m_temperature",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_resolve_variables_default_and_aliases(mod):
    assert mod._resolve_variables(None) == ["tp"]
    assert mod._resolve_variables(["t2m", "tp", "t2m"]) == ["t2m", "tp"]
    assert mod._resolve_variables(["2_m_temperature"]) == ["t2m"]
    assert mod._resolve_variables(["sst"]) == ["sst"]
    assert mod._resolve_variables(["wtmp"]) == ["sst"]
    assert mod._resolve_variables(["sea_surface_temperature"]) == ["sst"]
    with pytest.raises(UsageError, match="most used first"):
        mod._resolve_variables(["2m_temperature"])


def test_resolve_variables_space_comma_and_repeated_flags(mod):
    """One call can take `-v tp t2m`, `-v tp -v t2m`, or `-v tp,t2m`."""
    assert mod._resolve_variables([["tp", "t2m"]]) == ["tp", "t2m"]
    assert mod._resolve_variables([["tp"], ["t2m"]]) == ["tp", "t2m"]
    assert mod._resolve_variables(["tp,t2m"]) == ["tp", "t2m"]
    assert mod._resolve_variables([["tp,t2m", "sst"]]) == ["tp", "t2m", "sst"]


def test_parser_accepts_space_separated_variables(fetch):
    args = fetch.parser.parse_args(
        ["--date", "2026-01-01", "--bbox", "3/10/0/13", "-v", "tp", "t2m", "-o", "out.zarr"]
    )
    assert args.variable == [["tp", "t2m"]]
    args = fetch.parser.parse_args(
        [
            "--date",
            "2026-01-01",
            "--bbox",
            "3/10/0/13",
            "-v",
            "tp",
            "-v",
            "t2m",
            "-o",
            "out.zarr",
        ]
    )
    assert args.variable == [["tp"], ["t2m"]]


def test_variables_most_used_first(mod):
    names = list(mod.VARIABLES)
    assert names[:3] == ["tp", "t2m", "sst"]
    assert "skt" in names
    assert "ocu" in names
    assert "gh" in names
    assert "t" in names
    assert "q" in names
    assert "pv" in names


def test_build_request(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast")
    assert req["year"] == ["2026"]
    assert req["month"] == ["01"]
    assert req["day"] == ["15"]
    assert req["forecast_type"] == "control_forecast"
    assert req["area"] == [3.0, 10.0, 0.0, 13.0]
    assert req["variable"] == ["total_precipitation"]
    assert req["leadtime_hour"][0] == "0"
    assert req["leadtime_hour"][1] == "24"
    assert req["leadtime_hour"][-1] == "1104"
    assert req["level_type"] == "single_level"
    assert "pressure_level" not in req


def test_leadtime_hours_are_daily_through_day_46(mod):
    hours = [int(h) for h in mod.LEADTIME_HOURS]
    assert hours == list(range(0, 46 * 24 + 1, 24))


def test_build_request_daily_mean(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["t2m"])
    assert req["variable"] == ["2_m_temperature"]
    assert req["leadtime_hour"][0] == "0_24"
    assert "24_48" in req["leadtime_hour"]
    assert "144_168" in req["leadtime_hour"]
    assert req["leadtime_hour"][-1] == "1080_1104"
    assert "0" not in req["leadtime_hour"]


def test_build_request_sst(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["sst"])
    assert req["variable"] == ["sea_surface_temperature"]
    assert req["level_type"] == "single_level"
    assert req["leadtime_hour"][0] == "0_24"
    assert "pressure_level" not in req


def test_build_request_pressure_level(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["t"])
    assert req["variable"] == ["temperature"]
    assert req["level_type"] == "pressure_level"
    assert req["pressure_level"][0] == "1000"
    assert req["pressure_level"][-1] == "10"
    assert req["leadtime_hour"][0] == "0"


def test_build_request_specific_humidity_seven_levels(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["q"])
    assert req["pressure_level"] == ["1000", "925", "850", "700", "500", "300", "200"]


def test_build_request_potential_vorticity(mod):
    req = mod._build_request("2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["pv"])
    assert req["level_type"] == "potential_temperature"
    assert req["potential_temperature"] == ["320"]


def test_group_pressure_separate_from_surface(mod):
    groups = dict(mod._group_for_request(["tp", "t", "q"]))
    assert len(groups) == 3


def test_group_same_family_stays_one_request(mod):
    groups = dict(mod._group_for_request(["t2m", "sst", "tp"]))
    assert len(groups) == 2
    daily = next(names for key, names in groups.items() if key[0] == "daily")
    instant = next(names for key, names in groups.items() if key[0] == "instant")
    assert daily == ["t2m", "sst"]
    assert instant == ["tp"]


def test_build_request_multiple_daily_variables(mod):
    req = mod._build_request(
        "2026-01-15", [3.0, 10.0, 0.0, 13.0], "control_forecast", ["t2m", "sst"]
    )
    assert req["variable"] == ["2_m_temperature", "sea_surface_temperature"]
    assert req["leadtime_hour"][0] == "0_24"


def test_promote_vertical_isobaric(mod):
    ds = xr.Dataset(
        {"t": (("isobaricInhPa", "latitude"), [[1.0, 2.0], [3.0, 4.0]])},
        coords={"isobaricInhPa": [1000.0, 850.0], "latitude": [1.0, 2.0]},
    )
    out = mod._promote_vertical(ds)
    assert "vertical" in out.dims
    assert "isobaricInhPa" not in out.dims
    assert out["vertical"].attrs["units"] == "hPa"


def test_rename_grib_short_names(mod):
    ds = xr.Dataset({"2t": ("x", [1.0]), "noise": ("x", [0.0])})
    out = mod._rename_to_short(ds, ["t2m"])
    assert list(out.data_vars) == ["t2m"]


def test_rename_wtmp_to_sst(mod):
    ds = xr.Dataset({"wtmp": ("x", [290.0])})
    out = mod._rename_to_short(ds, ["sst"])
    assert list(out.data_vars) == ["sst"]


def test_split_wrapped_area(mod):
    assert mod._split_wrapped_area([3.0, 10.0, 0.0, 13.0]) == [[3.0, 10.0, 0.0, 13.0]]
    wrapped = mod._split_wrapped_area([3.0, 170.0, 0.0, -170.0])
    assert wrapped == [[3.0, 170.0, 0.0, 180.0], [3.0, -180.0, 0.0, -170.0]]


def test_standardize_mixed_tp_and_t2m(mod):
    steps = np.array([np.timedelta64(d, "D") for d in (0, 1, 2)])
    ds = xr.Dataset(
        {
            "tp": (("step", "latitude"), np.array([[0.0, 0.0], [2.0, 2.0], [5.0, 5.0]])),
            "t2m": (
                ("step", "latitude"),
                np.array([[280.0, 281.0], [282.0, 283.0], [284.0, 285.0]]),
            ),
        },
        coords={
            "time": np.datetime64("2026-01-01", "ns"),
            "step": steps,
            "latitude": [1.0, 2.0],
        },
    )
    ds["tp"].attrs.update(units="kg m-2")
    ds["t2m"].attrs.update(units="K")
    out = mod._standardize(ds)
    assert out.sizes["step"] == 2
    np.testing.assert_array_equal(
        np.asarray(out["step"].values).astype("timedelta64[D]"),
        np.array([0, 1], dtype="timedelta64[D]"),
    )
    assert out["tp"].attrs["units"] == "mm day-1"
    np.testing.assert_allclose(out["tp"].values, [[2.0, 2.0], [3.0, 3.0]])
    assert out["t2m"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(
        out["t2m"].values,
        np.array([[280.0, 281.0], [282.0, 283.0]]) - 273.15,
        rtol=1e-5,
    )


def test_standardize_sst_to_celsius(mod):
    steps = np.array([np.timedelta64(d, "D") for d in (1, 2)])
    ds = xr.Dataset(
        {"sst": (("step", "latitude"), np.array([[290.0, 291.0], [292.0, 293.0]]))},
        coords={
            "time": np.datetime64("2026-01-01", "ns"),
            "step": steps,
            "latitude": [1.0, 2.0],
        },
    )
    ds["sst"].attrs.update(units="K")
    out = mod._standardize(ds)
    assert out["sst"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(
        out["sst"].values, np.array([[290.0, 291.0], [292.0, 293.0]]) - 273.15
    )


def _catalog_surface_ds(*, n_lead=3, with_lead0_nan=True):
    inits = np.array([np.datetime64("2026-01-01T00:00")])
    leads = np.arange(n_lead) * np.timedelta64(1, "D")
    lats = [2.0, 1.0]
    lons = [11.0, 12.0]
    precip = np.full((1, n_lead, 2, 2, 2), 2e-6, dtype=np.float64)
    t2m = np.full((1, n_lead, 2, 2, 2), 20.0, dtype=np.float64)
    if with_lead0_nan:
        precip[:, 0] = np.nan
        t2m[:, 0] = np.nan
    return xr.Dataset(
        {
            "precipitation_surface": (
                ("init_time", "lead_time", "ensemble_member", "latitude", "longitude"),
                precip,
                {"units": "kg m-2 s-1", "standard_name": "precipitation_flux"},
            ),
            "average_temperature_2m": (
                ("init_time", "lead_time", "ensemble_member", "latitude", "longitude"),
                t2m,
                {"units": "degree_Celsius"},
            ),
            "wind_u_10m": (
                ("init_time", "lead_time", "ensemble_member", "latitude", "longitude"),
                np.ones((1, n_lead, 2, 2, 2)),
                {"units": "m s-1"},
            ),
        },
        coords={
            "init_time": inits,
            "lead_time": leads,
            "ensemble_member": [0, 1],
            "latitude": lats,
            "longitude": lons,
        },
    )


def _catalog_pressure_ds(*, n_lead=3):
    inits = np.array([np.datetime64("2026-01-01T00:00")])
    leads = np.arange(n_lead) * np.timedelta64(1, "D")
    return xr.Dataset(
        {
            "temperature": (
                (
                    "init_time",
                    "lead_time",
                    "ensemble_member",
                    "pressure_level",
                    "latitude",
                    "longitude",
                ),
                np.full((1, n_lead, 2, 2, 2, 2), 280.0),
                {"units": "K"},
            )
        },
        coords={
            "init_time": inits,
            "lead_time": leads,
            "ensemble_member": [0, 1],
            "pressure_level": [1000.0, 850.0],
            "latitude": [2.0, 1.0],
            "longitude": [11.0, 12.0],
        },
    )


def test_catalog_covers_mapped_fields_from_2026(mod):
    assert mod._catalog_covers(date(2026, 1, 1), ["tp", "t2m", "t"])
    assert not mod._catalog_covers(date(2025, 12, 31), ["tp"])
    assert not mod._catalog_covers(date(2026, 1, 1), ["pv"])
    assert not mod._catalog_covers(date(2026, 1, 1), ["tp", "mx2t6"])


def test_drop_empty_lead0_shifts_when_all_nan(mod):
    ds = _catalog_surface_ds()[["precipitation_surface"]]
    out = mod._drop_empty_lead0(ds)
    assert out.sizes["lead_time"] == 2
    np.testing.assert_array_equal(
        np.asarray(out["lead_time"].values).astype("timedelta64[D]"),
        np.array([0, 1], dtype="timedelta64[D]"),
    )


def test_drop_empty_lead0_keeps_instant_fields(mod):
    ds = _catalog_surface_ds()[["wind_u_10m"]]
    out = mod._drop_empty_lead0(ds)
    assert out.sizes["lead_time"] == 3


def test_catalog_fetch_writes_tp_without_creds(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY")
    }

    with patch.dict(os.environ, env, clear=True):
        with patch.object(mod, "_open_catalog", return_value=_catalog_surface_ds()):
            run_skill(
                fetch,
                "--date",
                "2026-01-01",
                "--bbox",
                "3/10/0/13",
                "-o",
                str(out),
            )

    assert Path(out).exists()
    written = xr.open_zarr(out, consolidated=True)
    assert "tp" in written
    assert "precipitation_surface" not in written
    assert written.attrs.get("weather_skills_source") == "ecmwf-s2s"
    assert written["tp"].attrs.get("units") == "mm day-1"
    assert written.sizes["step"] == 2
    assert "number" in written.dims
    assert load_history(out)[-1]["skill"] == "ecmwf-fetch"


def test_catalog_fetch_pressure_group(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    def open_catalog(group=None):
        if group == "pressure_level":
            return _catalog_pressure_ds()
        return _catalog_surface_ds()

    with patch.object(mod, "_open_catalog", side_effect=open_catalog):
        run_skill(
            fetch,
            "--date",
            "2026-01-01",
            "--bbox",
            "3/10/0/13",
            "-v",
            "t",
            "-o",
            str(out),
        )

    written = xr.open_zarr(out, consolidated=True)
    assert "t" in written
    assert "vertical" in written.dims
    assert written.sizes["step"] == 3  # instant field keeps lead 0
    assert written["t"].attrs.get("units") == "degree_Celsius"


def test_probe_latest_uses_catalog_init(capsys, fetch, mod):
    with patch.object(mod, "_open_catalog", return_value=_catalog_surface_ds()):
        run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-01-01"


# --- ECDS path with a fake client and synthetic cfgrib datasets -------------

_H = np.timedelta64(1, "h")


def _fake_grib(path):
    """Mimic cfgrib: daily means are labeled at their window end (endStep)."""
    name = Path(path).name
    cf = name.startswith("cf_")
    if "_daily_" in name:
        steps = np.arange(24, 24 * 4 + 1, 24) * _H  # 0_24 .. 72_96
        var, vals, units = "2t", 280.0 + np.arange(4), "K"
    elif "10lev" in name:
        steps = np.arange(0, 24 * 4 + 1, 24) * _H
        var, vals, units = "t", 250.0 + np.arange(5), "K"
    else:
        steps = np.arange(0, 24 * 4 + 1, 24) * _H
        var, vals, units = "tp", np.array([0.0, 2.0, 5.0, 9.0, 14.0]), "kg m-2"
    steps = steps.astype("timedelta64[ns]")
    init = np.datetime64("2025-06-02", "ns")
    dims = ["step", "latitude", "longitude"]
    data = np.broadcast_to(vals[:, None, None], (len(steps), 2, 2)).copy()
    coords = {
        "time": init,
        "step": steps,
        "latitude": [1.0, 0.0],
        "longitude": [34.0, 35.0],
        "valid_time": ("step", init + steps),
    }
    if "10lev" in name:
        data = data[:, None]
        dims.insert(1, "isobaricInhPa")
        coords["isobaricInhPa"] = [850.0]
    if cf:
        coords["number"] = 0
    else:
        data = np.stack([data, data])
        dims.insert(0, "number")
        coords["number"] = [1, 2]
    return xr.Dataset({var: (dims, data, {"units": units})}, coords=coords)


@pytest.fixture
def fake_ecds(monkeypatch, mod):
    import sys
    import types

    downloads = {"error": None}

    class Remote:
        results_ready = True

        def download(self, path):
            if downloads["error"] is not None:
                raise downloads["error"]
            Path(path).write_text("grib")

    class Client:
        def submit(self, name, request):
            return Remote()

    datastores = types.ModuleType("ecmwf.datastores")
    datastores.Client = Client
    processing = types.ModuleType("ecmwf.datastores.processing")
    processing.ProcessingFailedError = type("ProcessingFailedError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "ecmwf", types.ModuleType("ecmwf"))
    monkeypatch.setitem(sys.modules, "ecmwf.datastores", datastores)
    monkeypatch.setitem(sys.modules, "ecmwf.datastores.processing", processing)
    monkeypatch.setenv("ECMWF_DATASTORES_KEY", "dummy")
    monkeypatch.setattr(mod, "_open_grib", _fake_grib)
    return downloads


def _run_ecds(fetch, out, *variables, date_iso="2025-06-02"):
    run_skill(fetch, "--date", date_iso, "--bbox", "1/34/0/35", "-v", *variables, "-o", str(out))
    return xr.open_zarr(out, consolidated=True)


def test_ecds_tp_and_daily_mean_share_step_labels(tmp_path, fetch, fake_ecds):
    """Day-0 t2m mean and day-0 tp rate both sit at step 0 (not shifted a day)."""
    ds = _run_ecds(fetch, tmp_path / "o.zarr", "tp", "t2m")
    steps = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
    assert steps.tolist() == [0, 24, 48, 72]
    point = ds.isel(latitude=0, longitude=0).sel(number=0)
    np.testing.assert_allclose(point["tp"].values, [2.0, 3.0, 4.0, 5.0])
    np.testing.assert_allclose(point["t2m"].values, 280.0 + np.arange(4) - 273.15, rtol=1e-5)
    assert ds.sizes["number"] == 3


def test_ecds_daily_mean_alone_starts_at_step_zero(tmp_path, fetch, fake_ecds):
    ds = _run_ecds(fetch, tmp_path / "o.zarr", "t2m")
    steps = np.asarray(ds["step"].values).astype("timedelta64[h]").astype(int)
    assert steps.tolist() == [0, 24, 48, 72]
    assert ds["t2m"].attrs["data_interval"] == "1 day"


def test_ecds_control_only_field_keeps_number_dim(tmp_path, fetch, fake_ecds):
    ds = _run_ecds(fetch, tmp_path / "o.zarr", "t")
    assert ds["t"].dims[0] == "number"
    assert ds["number"].values.tolist() == [0]


def test_ecds_control_only_field_with_ensemble_field_is_nan_for_members(tmp_path, fetch, fake_ecds):
    ds = _run_ecds(fetch, tmp_path / "o.zarr", "tp", "t")
    assert ds["t"].dims[0] == "number"
    t = ds["t"].isel(latitude=0, longitude=0, step=0, vertical=0)
    assert np.isfinite(t.sel(number=0).values)
    assert np.isnan(t.sel(number=[1, 2]).values).all()


def test_ecds_download_failure_is_clean_error(tmp_path, fetch, fake_ecds, capsys):
    fake_ecds["error"] = ConnectionError("connection reset")
    with pytest.raises(SystemExit) as exc:
        _run_ecds(fetch, tmp_path / "o.zarr", "tp")
    assert exc.value.code == 1
    assert "ECDS download failed" in capsys.readouterr().err


def test_ecds_refuses_tp_with_other_accumulated_fields(tmp_path, fetch, fake_ecds):
    with pytest.raises(SystemExit) as exc:
        _run_ecds(fetch, tmp_path / "o.zarr", "tp", "cp")
    assert exc.value.code == 2


def test_ecds_embargo_date_fails_before_needing_credentials(tmp_path, fetch, capsys):
    """An init inside the 2-day embargo is refused without asking for the key."""
    recent = date.today().isoformat()
    env = {k: v for k, v in os.environ.items() if k != "ECMWF_DATASTORES_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--date",
                recent,
                "--bbox",
                "1/34/0/35",
                "-v",
                "pv",
                "-o",
                str(tmp_path / "o.zarr"),
            )
    assert exc.value.code == 1
    assert "embargo" in capsys.readouterr().err


def test_catalog_open_failure_is_clean_error(tmp_path, mod, fetch, capsys):
    import dynamical_catalog

    with patch.object(dynamical_catalog, "list", side_effect=ConnectionError("offline")):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch, "--date", "2026-01-01", "--bbox", "1/34/0/35", "-o", str(tmp_path / "o.zarr")
            )
    assert exc.value.code == 1
    assert "dynamical.org catalog" in capsys.readouterr().err

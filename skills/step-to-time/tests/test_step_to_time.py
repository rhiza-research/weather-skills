"""Correctness tests for step-to-time."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def step_to_time():
    return load_skill("step-to-time", "step_to_time").step_to_time


def _forecast_without_precip_totals(**kwargs):
    ds = make_forecast(name="t2m", **kwargs)
    ds["t2m"].attrs.update(units="K", standard_name="air_temperature", long_name="Temperature")
    return ds


def test_step_to_time_replaces_step_dim(tmp_path, step_to_time):
    src = write_zarr(
        _forecast_without_precip_totals(n_step=3, init="2026-01-01"), tmp_path / "in.zarr"
    )
    out = tmp_path / "out.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "step" not in ds.dims
    assert "time" in ds.dims
    assert ds.sizes["time"] == 3
    expected = np.array(
        ["2026-01-01", "2026-01-02", "2026-01-03"],
        dtype="datetime64[ns]",
    )
    assert np.array_equal(
        ds["time"].values.astype("datetime64[D]"), expected.astype("datetime64[D]")
    )
    assert load_history(out)[-1]["skill"] == "step-to-time"


def test_step_to_time_accepts_spatially_reduced_cube(tmp_path, step_to_time):
    ds = _forecast_without_precip_totals(n_step=3, init="2026-01-01", members=2)
    ds = ds.mean(["latitude", "longitude"])
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert "step" not in result.dims
    assert "time" in result.dims
    assert result.sizes["time"] == 3
    assert "number" in result.dims
    assert "latitude" not in result.dims
    assert "longitude" not in result.dims


def test_step_to_time_accepts_precip_totals(tmp_path, step_to_time):
    ds = make_forecast(n_step=3, init="2026-01-01")
    ds["tp"].attrs.update(
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="step: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert "time" in result.dims
    assert "sum" in result["tp"].attrs["cell_methods"]


def test_step_to_time_rewrites_cf_bounds(tmp_path, step_to_time):
    from weather_skills_core.units import stamp_data_interval

    ds = _forecast_without_precip_totals(n_step=4, init="2026-01-01")
    ds = ds.assign_coords(
        step=np.array([7, 10, 14, 21], dtype="timedelta64[D]").astype("timedelta64[ns]")
    )
    ds = stamp_data_interval(ds, dim="step")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(step_to_time, "-i", str(src), "-o", str(out))
    result = xr.open_zarr(out, consolidated=True)
    assert result["time"].attrs.get("bounds") == "time_bounds"
    assert "step_bounds" not in result.variables
    bounds = np.asarray(result["time_bounds"].values)
    assert bounds.shape == (4, 2)
    assert str(bounds[0, 0])[:10] == "2026-01-01"
    assert str(bounds[0, 1])[:10] == "2026-01-08"


@pytest.mark.parametrize(
    "steps, init",
    [
        ((2, 0, 1), "2026-08-24"),  # unsorted
        ((0, 1, 1), "2026-08-24"),  # duplicate lead
        ((0, 1, 2), "NaT"),  # missing init
    ],
)
def test_refuses_bad_step_or_init(tmp_path, step_to_time, steps, init):
    ds = xr.Dataset(
        {"tp": (("step", "latitude", "longitude"), np.ones((3, 1, 1)), {"units": "mm day-1"})},
        coords={
            "step": np.asarray(steps, dtype="timedelta64[D]").astype("timedelta64[ns]"),
            "time": np.datetime64(init, "ns"),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    src = tmp_path / "in.zarr"
    ds.to_zarr(src, mode="w", consolidated=True)
    with pytest.raises(SystemExit) as exc:
        run_skill(step_to_time, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_noleap_init_crosses_month_end(tmp_path, step_to_time):
    """A model-calendar init: 27 Feb + 2 days is 1 Mar in noleap (no 29 Feb)."""
    import cftime

    ds = xr.Dataset(
        {"tp": (("step", "latitude", "longitude"), np.ones((3, 1, 1)), {"units": "mm day-1"})},
        coords={
            "step": np.arange(3).astype("timedelta64[D]").astype("timedelta64[ns]"),
            "time": np.array(cftime.DatetimeNoLeap(2028, 2, 27), dtype=object),
            "latitude": [1.0],
            "longitude": [10.0],
        },
    )
    src = tmp_path / "in.zarr"
    ds.to_zarr(src, mode="w", consolidated=True)
    out = tmp_path / "out.zarr"
    run_skill(step_to_time, "-i", str(src), "-o", str(out))
    times = xr.open_zarr(out, consolidated=True, use_cftime=True)["time"].values
    assert [(t.month, t.day) for t in times] == [(2, 27), (2, 28), (3, 1)]


def _inits_at_one_lead(inits, lead_days):
    return xr.Dataset(
        {"tp": (("time", "number"), np.ones((len(inits), 2)), {"units": "mm day-1"})},
        coords={
            "time": np.asarray(inits, dtype="datetime64[ns]"),
            "step": np.timedelta64(lead_days, "D").astype("timedelta64[ns]"),
            "number": [0, 1],
        },
    )


def test_many_inits_at_one_lead_shift_to_valid_dates(tmp_path, step_to_time):
    ds = _inits_at_one_lead(["2025-07-02", "2025-07-03", "2025-07-05"], 13)
    ds = ds.assign_coords(
        step_bounds=("nv", np.array([13, 14], dtype="timedelta64[D]").astype("timedelta64[ns]"))
    )
    ds["step"].attrs["bounds"] = "step_bounds"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert result["time"].values.astype("datetime64[D]").astype(str).tolist() == [
        "2025-07-15",
        "2025-07-16",
        "2025-07-18",
    ]
    assert result["step"].values == np.timedelta64(13, "D")
    assert result["time"].attrs.get("bounds") == "time_bounds"
    bounds = result["time_bounds"].values.astype("datetime64[D]").astype(str)
    assert bounds[0].tolist() == ["2025-07-15", "2025-07-16"]
    assert np.array_equal(result["tp"].values, ds["tp"].values)


def test_many_inits_and_many_leads_exits(tmp_path, step_to_time):
    ds = _inits_at_one_lead(["2025-07-02", "2025-07-03"], 13)
    ds = ds.drop_vars("step").expand_dims(step=[np.timedelta64(13, "D").astype("timedelta64[ns]")])
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(step_to_time, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_many_inits_rerun_on_own_output_exits(tmp_path, step_to_time):
    ds = _inits_at_one_lead(["2025-07-02", "2025-07-03"], 13)
    ds["time"].attrs["standard_name"] = "forecast_reference_time"
    src = write_zarr(ds, tmp_path / "in.zarr")
    once = tmp_path / "once.zarr"
    run_skill(step_to_time, "-i", str(src), "-o", str(once))
    assert xr.open_zarr(once)["time"].attrs["standard_name"] == "time"
    with pytest.raises(SystemExit) as exc:
        run_skill(step_to_time, "-i", str(once), "-o", str(tmp_path / "twice.zarr"))
    assert exc.value.code == 2


def test_many_inits_keep_values_attrs_and_time_of_day(tmp_path, step_to_time):
    ds = _inits_at_one_lead(["2025-07-02T12", "2025-07-03T12"], 2)
    ds["tp"].values = np.arange(4.0).reshape(2, 2)
    ds["tp"].attrs["data_interval"] = "1 day"
    ds = ds.assign_coords(
        valid_time=("time", ds["time"].values),
        time_bounds=(("time", "nv"), np.stack([ds["time"].values] * 2, axis=1)),
    )
    ds["time"].attrs["bounds"] = "time_bounds"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(step_to_time, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    assert result["time"].values.astype(str).tolist() == [
        "2025-07-04T12:00:00.000000000",
        "2025-07-05T12:00:00.000000000",
    ]
    assert np.array_equal(result["tp"].values, np.arange(4.0).reshape(2, 2))
    assert result["tp"].attrs["data_interval"] == "1 day"
    assert "valid_time" not in result.variables
    assert "time_bounds" not in result.variables
    assert "bounds" not in result["time"].attrs


def test_many_inits_on_stations(tmp_path, step_to_time):
    ds = xr.Dataset(
        {"tp": (("time", "station"), np.ones((2, 3)), {"units": "mm day-1"})},
        coords={
            "time": np.array(["2025-07-02", "2025-07-03"], dtype="datetime64[ns]"),
            "step": np.timedelta64(1, "D").astype("timedelta64[ns]"),
            "station": ["a", "b", "c"],
            "lat": ("station", [0.0, 1.0, 2.0]),
            "lon": ("station", [30.0, 31.0, 32.0]),
        },
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(step_to_time, "-i", str(src), "-o", str(out))
    result = xr.open_zarr(out, consolidated=True)
    assert result["time"].values.astype("datetime64[D]").astype(str).tolist() == [
        "2025-07-03",
        "2025-07-04",
    ]
    assert result["lat"].dims == ("station",)


def test_many_noleap_inits_cross_month_end(tmp_path, step_to_time):
    import cftime

    inits = [cftime.DatetimeNoLeap(2028, 2, d) for d in (26, 27, 28)]
    ds = xr.Dataset(
        {"tp": (("time",), np.ones(3), {"units": "mm day-1"})},
        coords={
            "time": np.array(inits, dtype=object),
            "step": np.timedelta64(2, "D").astype("timedelta64[ns]"),
        },
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(step_to_time, "-i", str(src), "-o", str(out))
    coder = xr.coders.CFDatetimeCoder(use_cftime=True)
    times = xr.open_zarr(out, consolidated=True, decode_times=coder)["time"].values
    assert [(t.month, t.day) for t in times] == [(2, 28), (3, 1), (3, 2)]


@pytest.mark.parametrize(
    "inits",
    [
        ["2025-07-03", "2025-07-02"],  # unsorted
        ["2025-07-02", "2025-07-02"],  # duplicate init
        ["2025-07-02", "NaT"],  # missing init
    ],
)
def test_many_inits_refuse_bad_init_axis(tmp_path, step_to_time, inits):
    src = write_zarr(_inits_at_one_lead(inits, 13), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(step_to_time, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_time_axis_without_step_exits(tmp_path, step_to_time):
    ds = _inits_at_one_lead(["2025-07-02", "2025-07-03"], 13).drop_vars("step")
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(step_to_time, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2

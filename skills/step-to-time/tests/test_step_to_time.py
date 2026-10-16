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

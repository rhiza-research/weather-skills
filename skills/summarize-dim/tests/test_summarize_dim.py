"""Correctness tests for summarize-dim."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def summarize_dim():
    return load_skill("summarize-dim", "summarize_dim").summarize_dim


def test_mean_collapses_time(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(n_time=3, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert ds["precip"].values == pytest.approx(2.0)
    assert load_history(out)[-1]["skill"] == "summarize-dim"


def test_sum_collapses_longitude(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim, "-i", str(src), "-o", str(out), "--dim", "longitude", "--method", "sum"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "longitude" not in ds.dims
    assert ds["precip"].sizes["latitude"] == 3


def _celsius(lats=(0.0, 60.0), lons=(10.0, 11.0)):
    import numpy as np

    ds = make_gridded(name="t2m", lats=lats, lons=lons)
    # 10 °C at the equator row, 30 °C at 60°N (cos weights 1 and 0.5).
    ds["t2m"].values[:] = np.array([10.0, 30.0])[None, :, None]
    ds["t2m"].attrs.update(units="degree_Celsius", standard_name="air_temperature")
    return ds


def test_lat_weighted_mean_celsius(tmp_path, summarize_dim):
    src = write_zarr(_celsius(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "latitude",
        "--method",
        "mean",
        "--lat-weighted",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "latitude" not in ds.dims
    # (1 * 10 + 0.5 * 30) / 1.5 = 16.67 °C — not 290 K, and not 16.67 K.
    assert ds["t2m"].values == pytest.approx(50.0 / 3.0)
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
    assert ds["t2m"].attrs["standard_name"] == "air_temperature"


def test_lat_weighted_mean_celsius_over_lat_and_lon(tmp_path, summarize_dim):
    src = write_zarr(_celsius(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "latitude",
        "--dim",
        "longitude",
        "--method",
        "mean",
        "--lat-weighted",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["t2m"].dims == ("time",)
    assert ds["t2m"].values == pytest.approx(50.0 / 3.0)


def test_lat_weighted_mean_precip_unchanged(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "latitude",
        "--method",
        "mean",
        "--lat-weighted",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].values == pytest.approx(2.0)
    assert units_equal(ds["precip"].attrs["units"], "mm day-1")


def test_accepts_precip_totals(tmp_path, summarize_dim):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    result = xr.open_zarr(out, consolidated=True)
    assert "time" not in result.dims
    assert "sum" in result["precip"].attrs["cell_methods"]

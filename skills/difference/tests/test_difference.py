"""Correctness tests for difference."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def difference():
    return load_skill("difference", "difference").difference


def test_difference_subtracts_b_from_a(tmp_path, difference):
    a = write_zarr(make_gridded(fill=5.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "out.zarr"

    run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out))

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].values == pytest.approx(3.0)
    assert load_history(out)[-1]["skill"] == "difference"


def test_difference_requires_two_inputs(tmp_path, difference):
    a = write_zarr(make_gridded(), tmp_path / "a.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(difference, "-i", str(a), "-o", str(out))
    assert exc.value.code == 2


def test_partly_overlapping_grids_return_only_the_overlap(tmp_path, difference):
    """Pins current behaviour: inner-join alignment keeps only shared points, with no warning."""
    a = make_gridded(n_time=2)  # lat 1,2,3 x lon 10..13
    b = make_gridded(n_time=2, lats=(1.0, 1.5, 2.0, 2.5), lons=(10.0, 10.5, 11.0, 11.5))
    out = tmp_path / "d.zarr"
    run_skill(
        difference,
        "-i",
        str(write_zarr(a, tmp_path / "a.zarr")),
        "-i",
        str(write_zarr(b, tmp_path / "b.zarr")),
        "-o",
        str(out),
    )
    res = xr.open_zarr(out)
    assert res.sizes["latitude"] == 2 and res.sizes["longitude"] == 2

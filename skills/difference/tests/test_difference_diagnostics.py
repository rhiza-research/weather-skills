"""Remaining diagnostics recovered from PR #131."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def difference():
    return load_skill("difference", "difference").difference


def _with_units(ds, units="mm day-1"):
    for v in ds.data_vars:
        ds[v].attrs["units"] = units
    return ds


def _inputs(tmp_path, a, b):
    return (
        write_zarr(a, tmp_path / "a.zarr"),
        write_zarr(b, tmp_path / "b.zarr"),
        tmp_path / "out.zarr",
    )


def test_equal_size_coordinateless_dim_warns_positional(tmp_path, difference, capsys):
    a = xr.Dataset({"precip": (("gauge",), np.array([1.0, 2.0, 3.0]), {"units": "mm day-1"})})
    b = xr.Dataset({"precip": (("gauge",), np.array([0.5, 0.5, 0.5]), {"units": "mm day-1"})})
    pa, pb, out = _inputs(tmp_path, a, b)
    run_skill(difference, "-i", str(pa), "-i", str(pb), "-o", str(out))
    assert "positionally" in capsys.readouterr().err
    np.testing.assert_allclose(xr.open_zarr(out)["precip"].values, [0.5, 1.5, 2.5])


def test_unselected_variables_are_dropped_with_a_note(tmp_path, difference, capsys):
    a = _with_units(make_gridded())
    a["extra"] = a["precip"] * 2
    a, b, out = _inputs(tmp_path, a, _with_units(make_gridded()))
    run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out))
    assert "dropping data variable(s) ['extra']" in capsys.readouterr().err
    assert list(xr.open_zarr(out).data_vars) == ["precip"]

"""Correctness tests for mask."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mask():
    return load_skill("mask", "mask").mask


def _stations(fill, n_time=4, n_points=3):
    ds = make_point_obs(n_time=n_time, n_points=n_points, fill=fill)
    ds["precip"].values = ds["precip"].values * 1.0
    return ds


def _run(mask, tmp_path, ds, *refs):
    """refs: (flag, Dataset) pairs."""
    args = ["-i", str(write_zarr(ds, tmp_path / "in.zarr"))]
    for n, (flag, ref) in enumerate(refs):
        args += [flag, str(write_zarr(ref, tmp_path / f"ref{n}.zarr"))]
    out = tmp_path / "out.zarr"
    run_skill(mask, *args, "-o", str(out))
    return xr.open_zarr(out, consolidated=True), out


def test_nan_in_any_reference_removes_the_point(tmp_path, mask):
    ds_in = _stations(5.0)
    a, b = _stations(0.0), _stations(2.0)
    a["precip"][0, 1] = np.nan
    b["precip"][2, 3] = np.nan
    ds, out = _run(mask, tmp_path, ds_in, ("--valid-in", a), ("--valid-in", b))
    expected = np.full((3, 4), 5.0)
    expected[0, 1] = expected[2, 3] = np.nan
    # Zero rainfall in a reference is a value, so it stays valid.
    np.testing.assert_array_equal(ds["precip"].values, expected)
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert load_history(out)[-1]["skill"] == "mask"


def test_labels_missing_from_a_reference_are_removed(tmp_path, mask):
    ref = _stations(1.0).isel(time=slice(0, 2), point_id=slice(0, 2))
    ds, _ = _run(mask, tmp_path, _stations(5.0), ("--valid-in", ref))
    assert ds.sizes == {"point_id": 3, "time": 4}
    assert int(ds["precip"].notnull().sum()) == 4
    assert np.isnan(ds["precip"].sel(point_id="S2")).all()


def test_true_in_mask_without_time_applies_to_every_date(tmp_path, mask):
    ds_in = make_gridded(n_time=3, fill=7.0)
    land = make_gridded(n_time=1, fill=1.0).isel(time=0, drop=True)
    land["precip"][0, :] = 0.0
    land["precip"][1, 0] = np.nan
    ds, _ = _run(mask, tmp_path, ds_in, ("--true-in", land))
    kept = ds["precip"].notnull()
    assert not kept.isel(latitude=0).any()
    assert not kept.isel(latitude=1, longitude=0).any()
    assert kept.isel(latitude=2).all()
    assert float(ds["precip"].max()) == 7.0


@pytest.mark.parametrize("case", ["no_refs", "bad_variable", "extra_dim", "no_overlap"])
def test_bad_arguments_are_refused(tmp_path, mask, case):
    ds_in = _stations(5.0)
    refs = {
        "no_refs": [],
        "bad_variable": [("--valid-in", _stations(1.0))],
        "extra_dim": [("--valid-in", make_gridded())],
        "no_overlap": [("--valid-in", make_point_obs(start="2027-01-01"))],
    }[case]
    args = ["-v", "nope"] if case == "bad_variable" else []
    src = write_zarr(ds_in, tmp_path / "in.zarr")
    argv = ["-i", str(src), *args]
    for n, (flag, ref) in enumerate(refs):
        argv += [flag, str(write_zarr(ref, tmp_path / f"ref{n}.zarr"))]
    with pytest.raises(SystemExit) as exc:
        run_skill(mask, *argv, "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == (1 if case == "no_overlap" else 2)

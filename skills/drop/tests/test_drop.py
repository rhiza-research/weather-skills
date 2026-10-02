"""Correctness tests for drop."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def drop():
    return load_skill("drop", "drop").drop


def _run(drop, tmp_path, ds, *names):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    args = [a for n in names for a in ("--name", n)]
    run_skill(drop, "-i", str(src), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True), out


def _usage_error(drop, tmp_path, ds, *names):
    with pytest.raises(SystemExit) as exc:
        _run(drop, tmp_path, ds, *names)
    assert exc.value.code == 2


def _with_leftovers():
    ds = make_gridded()
    ds = ds.assign_coords(spatial_ref=0, surface=0.0, valid_time=("time", ds["time"].values))
    ds["precip"].attrs["grid_mapping"] = "spatial_ref"
    ds = ds.assign(other=ds["precip"] * 2)
    return ds


def test_drop_data_var_and_coords_together(tmp_path, drop):
    ds, out = _run(drop, tmp_path, _with_leftovers(), "other", "surface", "valid_time")
    assert "other" not in ds.variables
    assert "surface" not in ds.variables
    assert "valid_time" not in ds.variables
    assert "precip" in ds.data_vars
    assert load_history(out)[-1]["skill"] == "drop"


def test_drop_spatial_ref_removes_grid_mapping(tmp_path, drop):
    ds, _ = _run(drop, tmp_path, _with_leftovers(), "spatial_ref")
    assert "spatial_ref" not in ds.variables
    assert "grid_mapping" not in ds["precip"].attrs


def test_drop_coord_takes_its_bounds(tmp_path, drop):
    base = make_gridded()
    base = base.assign_coords(
        valid_time=("time", base.time.values),
        vt_bounds=(("time", "nv"), np.stack([base.time.values] * 2, axis=1)),
    )
    base["valid_time"].attrs["bounds"] = "vt_bounds"
    ds, _ = _run(drop, tmp_path, base, "valid_time")
    assert "vt_bounds" not in ds.variables


def test_dropped_output_concats_with_a_clean_dataset(tmp_path, drop):
    ds, _ = _run(drop, tmp_path, _with_leftovers(), "spatial_ref", "surface", "valid_time", "other")
    clean = make_gridded(start="2026-01-03")
    combined = xr.concat([ds.load(), clean], dim="time")
    assert combined.sizes["time"] == 4


def test_drop_index_coord_is_refused(tmp_path, drop, capsys):
    _usage_error(drop, tmp_path, make_gridded(), "latitude")
    assert "select" in capsys.readouterr().err


def test_drop_missing_name_is_refused(tmp_path, drop):
    _usage_error(drop, tmp_path, make_gridded(), "nope")


def test_drop_last_data_var_is_refused(tmp_path, drop):
    _usage_error(drop, tmp_path, make_gridded(), "precip")

"""Correctness tests for edit-coords."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def edit_coords():
    return load_skill("edit-coords", "edit_coords").edit_coords


def _run(edit_coords, tmp_path, ds, *args):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(edit_coords, "-i", str(src), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True), out


def _usage_error(edit_coords, tmp_path, ds, *args):
    with pytest.raises(SystemExit) as exc:
        _run(edit_coords, tmp_path, ds, *args)
    assert exc.value.code == 2


def test_rename_data_variable_keeps_attrs_and_stamps_history(tmp_path, edit_coords):
    ds, out = _run(edit_coords, tmp_path, make_gridded(), "--rename", "precip=rain")
    assert "rain" in ds.data_vars and "precip" not in ds.data_vars
    assert ds["rain"].attrs["standard_name"] == "lwe_precipitation_rate"
    entry = load_history(out)[-1]
    assert entry["skill"] == "edit-coords"
    assert entry["args"]["rename"] == ["precip=rain"]


def test_rename_dim_coord_renames_the_dim(tmp_path, edit_coords):
    ds, _ = _run(
        edit_coords,
        tmp_path,
        make_gridded(),
        "--rename",
        "latitude=lat",
        "--rename",
        "longitude=lon",
    )
    assert {"lat", "lon"} <= set(ds.dims)
    assert "latitude" not in ds.variables


def test_rename_dim_without_coordinate(tmp_path, edit_coords):
    base = make_gridded()
    base = base.assign(extra=(("time", "ens"), np.ones((2, 3))))
    ds, _ = _run(edit_coords, tmp_path, base, "--rename", "ens=member")
    assert "member" in ds.dims and "ens" not in ds.dims


def test_alias_renames_keep_forecast_type(tmp_path, edit_coords):
    ds, _ = _run(
        edit_coords,
        tmp_path,
        make_forecast(members=2),
        "--rename",
        "number=member",
        "--rename",
        "step=prediction_timedelta",
    )
    assert {"member", "prediction_timedelta"} <= set(ds.dims)


def test_same_name_rename_is_a_noop(tmp_path, edit_coords):
    ds, out = _run(edit_coords, tmp_path, make_gridded(), "--rename", "precip=precip")
    assert "precip" in ds.data_vars
    assert load_history(out)[-1]["skill"] == "edit-coords"


def test_rename_carries_bounds_and_cf_refs(tmp_path, edit_coords):
    base = make_gridded()
    base = base.assign_coords(
        time_bounds=(("time", "nv"), np.stack([base.time.values] * 2, axis=1)),
        valid_time=("time", base.time.values),
    )
    base["time"].attrs["bounds"] = "time_bounds"
    base["precip"].attrs["ancillary_variables"] = "valid_time"
    ds, _ = _run(edit_coords, tmp_path, base, "--rename", "time=t", "--rename", "valid_time=vt")
    assert "t_bounds" in ds.variables and "time_bounds" not in ds.variables
    assert ds["t"].attrs["bounds"] == "t_bounds"
    assert ds["precip"].attrs["ancillary_variables"] == "vt"


def test_swap_then_rename_station_dim_to_point_id(tmp_path, edit_coords):
    base = make_point_obs().rename({"point_id": "station"})
    base = base.assign_coords(station_id=("station", ["A1", "B2"]))
    ds, _ = _run(
        edit_coords,
        tmp_path,
        base,
        "--swap-dims",
        "station=station_id",
        "--rename",
        "station_id=point_id",
        "--rename",
        "station=station_name",
    )
    assert "point_id" in ds.dims
    assert list(ds["point_id"].values) == ["A1", "B2"]
    assert ds["station_name"].dims == ("point_id",)


def test_swap_keeps_old_dim_as_coordinate(tmp_path, edit_coords):
    base = make_gridded()
    base = base.assign(bands=(("time", "band"), np.ones((2, 2))))
    base = base.assign_coords(band=[0, 1], band_name=("band", ["red", "nir"]))
    ds, _ = _run(edit_coords, tmp_path, base, "--swap-dims", "band=band_name")
    assert "band_name" in ds.dims
    assert ds["band"].dims == ("band_name",)


def test_swap_off_an_ontology_dim_is_refused(tmp_path, edit_coords):
    base = make_gridded().assign_coords(day=("time", [10, 11]))
    _usage_error(edit_coords, tmp_path, base, "--swap-dims", "time=day")


def test_rename_onto_existing_name_is_refused(tmp_path, edit_coords):
    _usage_error(edit_coords, tmp_path, make_gridded(), "--rename", "precip=time")


def test_rename_missing_name_is_refused(tmp_path, edit_coords):
    _usage_error(edit_coords, tmp_path, make_gridded(), "--rename", "nope=x")


def test_malformed_pair_is_refused(tmp_path, edit_coords):
    _usage_error(edit_coords, tmp_path, make_gridded(), "--rename", "precip")


def test_two_renames_onto_one_name_are_refused(tmp_path, edit_coords):
    base = make_gridded().assign(other=make_gridded()["precip"])
    _usage_error(edit_coords, tmp_path, base, "--rename", "precip=x", "--rename", "other=x")


def test_no_edits_is_refused(tmp_path, edit_coords):
    _usage_error(edit_coords, tmp_path, make_gridded())


def test_swap_onto_non_1d_coord_is_refused(tmp_path, edit_coords):
    base = make_gridded()
    base = base.assign_coords(mask=(("latitude", "longitude"), np.zeros((3, 4))))
    _usage_error(edit_coords, tmp_path, base, "--swap-dims", "latitude=mask")


def test_swap_onto_duplicate_values_is_refused(tmp_path, edit_coords):
    base = make_point_obs(n_points=2).assign_coords(region=("point_id", ["X", "X"]))
    _usage_error(edit_coords, tmp_path, base, "--swap-dims", "point_id=region")


def test_forecast_step_swap_is_refused_with_step_to_time_hint(tmp_path, edit_coords, capsys):
    base = make_forecast()
    base = base.assign_coords(valid_time=("step", base.time.values + base.step.values))
    _usage_error(edit_coords, tmp_path, base, "--swap-dims", "step=valid_time")
    assert "step-to-time" in capsys.readouterr().err


def test_renaming_lat_without_cf_attrs_to_unknown_name_is_refused(tmp_path, edit_coords):
    base = make_gridded()
    base["latitude"].attrs = {}
    _usage_error(edit_coords, tmp_path, base, "--rename", "latitude=y_coord")


def test_renaming_cf_tagged_lat_stays_recognised(tmp_path, edit_coords):
    # standard_name=latitude keeps lat detectable under any name.
    ds, _ = _run(edit_coords, tmp_path, make_gridded(), "--rename", "latitude=y_coord")
    assert "y_coord" in ds.dims


def test_two_way_rename_exchanges_names(tmp_path, edit_coords):
    base = make_gridded().assign(other=make_gridded(fill=2.0)["precip"])
    ds, _ = _run(
        edit_coords, tmp_path, base, "--rename", "precip=other", "--rename", "other=precip"
    )
    assert float(ds["precip"].max()) == 2.0 and float(ds["other"].max()) == 1.0

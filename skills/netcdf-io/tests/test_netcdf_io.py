"""Correctness tests for netcdf-io."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def netcdf_io():
    return load_skill("netcdf-io", "netcdf_io").netcdf_io


def _open(path):
    return xr.open_zarr(path, consolidated=True)


def test_grid_round_trip_keeps_values_attrs_and_history(tmp_path, netcdf_io):
    ds = make_gridded(n_time=3)
    ds["precip"].values[:] = np.random.default_rng(0).random(ds["precip"].shape)
    ds["precip"].values[1, 0, 0] = np.nan
    ds["precip"].attrs["aggregation_period"] = "1D"
    src = write_zarr(ds, tmp_path / "in.zarr")
    nc = tmp_path / "out.nc"
    back = tmp_path / "back.zarr"

    run_skill(netcdf_io, "-i", str(src), "-o", str(nc))
    with xr.open_dataset(nc) as out:
        assert out["precip"].attrs["aggregation_period"] == "1D"
        assert out["precip"].encoding["zlib"]
        assert "weather_skills_input_path" not in out.attrs
    run_skill(netcdf_io, "--netcdf", str(nc), "-o", str(back))

    res = _open(back)
    assert res["precip"].dims == ("time", "latitude", "longitude")
    np.testing.assert_allclose(res["precip"].values, ds["precip"].values, equal_nan=True)
    assert units_equal(res["precip"].attrs["units"], "mm day-1")
    assert load_history(back)[-1]["skill"] == "netcdf-io"
    assert res.attrs["weather_skills_source"] == "netcdf:out.nc"


def test_export_carries_upstream_history(tmp_path, netcdf_io):
    mid = tmp_path / "mid.zarr"
    nc = tmp_path / "out.nc"
    # Give the Zarr a history by reading a NetCDF once.
    make_gridded().to_netcdf(tmp_path / "seed.nc")
    run_skill(netcdf_io, "--netcdf", str(tmp_path / "seed.nc"), "-o", str(mid))

    run_skill(netcdf_io, "-i", str(mid), "-o", str(nc))

    with xr.open_dataset(nc) as out:
        history = json.loads(out.attrs["weather_skills_history"])
    assert history == load_history(mid)


def test_forecast_and_stations_round_trip(tmp_path, netcdf_io):
    fc = make_forecast(n_step=4, members=2)
    stations = make_point_obs(n_points=3)
    for name, ds in (("fc", fc), ("st", stations)):
        src = write_zarr(ds, tmp_path / f"{name}.zarr")
        nc = tmp_path / f"{name}.nc"
        back = tmp_path / f"{name}_back.zarr"
        run_skill(netcdf_io, "-i", str(src), "-o", str(nc))
        run_skill(netcdf_io, "--netcdf", str(nc), "-o", str(back))
        res = _open(back)
        assert dict(res.sizes) == dict(ds.sizes)
        for coord in ds.coords:
            np.testing.assert_array_equal(res[coord].values, ds[coord].values)
    assert _open(tmp_path / "fc_back.zarr")["step"].dtype == np.dtype("timedelta64[ns]")


def test_reads_several_files_joined_on_time(tmp_path, netcdf_io):
    full = make_gridded(n_time=4)
    full["precip"].values[:] = np.arange(full["precip"].size).reshape(full["precip"].shape)
    paths = []
    for i in range(4):
        path = tmp_path / f"day{i}.nc"
        full.isel(time=[i]).to_netcdf(path)
        paths += ["--netcdf", str(path)]
    out = tmp_path / "out.zarr"

    run_skill(netcdf_io, *paths, "-o", str(out))

    res = _open(out)
    np.testing.assert_array_equal(res["time"].values, full["time"].values)
    np.testing.assert_allclose(res["precip"].values, full["precip"].values)
    assert res.attrs["weather_skills_source"] == "netcdf:day0.nc,day1.nc,day2.nc,day3.nc"


def test_refuses_files_that_overlap_in_time(tmp_path, netcdf_io, capsys):
    full = make_gridded(n_time=4)
    full.isel(time=[0, 1, 2]).to_netcdf(tmp_path / "a.nc")
    full.isel(time=[2, 3]).to_netcdf(tmp_path / "b.nc")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            netcdf_io, "--netcdf", str(tmp_path / "a.nc"), "--netcdf", str(tmp_path / "b.nc"),
            "-o", str(tmp_path / "out.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2
    assert "2026-01-03" in capsys.readouterr().err


def test_merges_files_with_the_same_coords_and_other_variables(tmp_path, netcdf_io):
    ds = make_gridded(n_time=2)
    ds.to_netcdf(tmp_path / "a.nc")
    ds.rename(precip="t2m").to_netcdf(tmp_path / "b.nc")
    out = tmp_path / "out.zarr"
    run_skill(
        netcdf_io, "--netcdf", str(tmp_path / "a.nc"), "--netcdf", str(tmp_path / "b.nc"),
        "-o", str(out),
    )  # fmt: skip
    assert set(_open(out).data_vars) == {"precip", "t2m"}


def test_export_writes_bool_and_other_attrs(tmp_path, netcdf_io):
    ds = make_gridded(n_time=2)
    ds.attrs.update(flag=True, extra={"a": 1})
    ds["precip"].attrs.update(masked=np.bool_(False), flags=[True, False])
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.nc"

    run_skill(netcdf_io, "-i", str(src), "-o", str(out))

    res = xr.open_dataset(out)
    assert res.attrs["flag"] == 1
    assert res.attrs["extra"] == "{'a': 1}"
    assert res["precip"].attrs["masked"] == 0
    np.testing.assert_array_equal(res["precip"].attrs["flags"], [1, 0])
    res.close()


def test_read_wraps_0_360_longitude_and_keeps_variable(tmp_path, netcdf_io):
    ds = make_gridded(lons=(0.0, 90.0, 180.0, 270.0))
    ds["t2m"] = ds["precip"] * 0 + 290.0
    ds["t2m"].attrs = {"units": "K"}
    ds.to_netcdf(tmp_path / "in.nc")
    out = tmp_path / "out.zarr"

    run_skill(netcdf_io, "--netcdf", str(tmp_path / "in.nc"), "-o", str(out), "-v", "t2m")

    res = _open(out)
    assert list(res.data_vars) == ["t2m"]
    np.testing.assert_array_equal(res["longitude"].values, [-180.0, -90.0, 0.0, 90.0])


def test_units_fills_a_missing_units_attr(tmp_path, netcdf_io, capsys):
    ds = make_gridded()
    ds["precip"].attrs = {}
    ds.to_netcdf(tmp_path / "in.nc")
    bare, filled = tmp_path / "bare.zarr", tmp_path / "filled.zarr"

    run_skill(netcdf_io, "--netcdf", str(tmp_path / "in.nc"), "-o", str(bare))
    assert "no units for precip" in capsys.readouterr().err
    run_skill(
        netcdf_io, "--netcdf", str(tmp_path / "in.nc"), "-o", str(filled), "--units", "precip=mm"
    )

    assert units_equal(_open(filled)["precip"].attrs["units"], "mm")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-o", "out.zarr"], "exactly one"),
        (["--netcdf", "in.nc", "-o", "out.nc"], "should end in .zarr"),
        (["--netcdf", "missing.nc", "-o", "out.zarr"], "not found"),
        (["--netcdf", "in.nc", "-o", "out.zarr", "-v", "nope"], "not in NetCDF"),
        (["--netcdf", "in.nc", "-o", "out.zarr", "--units", "precip"], "VAR=UNITS"),
        (["--netcdf", "in.nc", "-o", "out.zarr", "--units", "nope=mm"], "not in NetCDF"),
    ],
)
def test_usage_errors(tmp_path, netcdf_io, monkeypatch, capsys, argv, message):
    monkeypatch.chdir(tmp_path)
    make_gridded().to_netcdf(tmp_path / "in.nc")

    with pytest.raises(SystemExit) as exc:
        run_skill(netcdf_io, *argv)

    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_export_needs_nc_suffix(tmp_path, netcdf_io, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")

    with pytest.raises(SystemExit):
        run_skill(netcdf_io, "-i", str(src), "-o", str(tmp_path / "out.zarr"))

    assert "should end in .nc" in capsys.readouterr().err


def test_read_decodes_char_array_station_names(tmp_path, netcdf_io):
    import netCDF4

    with netCDF4.Dataset(tmp_path / "in.nc", "w") as nc:
        nc.createDimension("station", 2)
        nc.createDimension("strlen", 8)
        name = nc.createVariable("station_name", "S1", ("station", "strlen"))
        name[:] = netCDF4.stringtochar(np.array(["Nairobi", "Mombasa"], dtype="S8"))
        rain = nc.createVariable("precip", "f4", ("station",))
        rain.units = "mm"
        rain.coordinates = "station_name"
        rain[:] = [1, 2]
    run_skill(netcdf_io, "--netcdf", str(tmp_path / "in.nc"), "-o", str(tmp_path / "out.zarr"))
    assert list(_open(tmp_path / "out.zarr")["station_name"].values) == ["Nairobi", "Mombasa"]


def test_read_sorts_time_given_out_of_order(tmp_path, netcdf_io):
    ds = make_gridded(n_time=3)
    ds["precip"].values[:] = np.arange(3)[:, None, None]
    ds.isel(time=[2, 0, 1]).to_netcdf(tmp_path / "in.nc")
    run_skill(netcdf_io, "--netcdf", str(tmp_path / "in.nc"), "-o", str(tmp_path / "out.zarr"))
    res = _open(tmp_path / "out.zarr")
    np.testing.assert_array_equal(res["time"].values, ds["time"].values)
    np.testing.assert_allclose(res["precip"].values[:, 0, 0], [0, 1, 2])


def test_read_refuses_a_time_given_twice_in_one_file(tmp_path, netcdf_io, capsys):
    make_gridded(n_time=3).isel(time=[0, 1, 1]).to_netcdf(tmp_path / "in.nc")
    with pytest.raises(SystemExit) as exc:
        run_skill(netcdf_io, "--netcdf", str(tmp_path / "in.nc"), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2
    assert "appear more than once" in capsys.readouterr().err


def test_read_refuses_a_file_that_is_not_netcdf(tmp_path, netcdf_io, capsys):
    (tmp_path / "in.nc").write_text("not netcdf\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(netcdf_io, "--netcdf", str(tmp_path / "in.nc"), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2
    assert "cannot read" in capsys.readouterr().err


def test_export_refuses_a_dataset_with_no_data_variables(tmp_path, netcdf_io, capsys):
    src = write_zarr(make_gridded().drop_vars("precip"), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(netcdf_io, "-i", str(src), "-o", str(tmp_path / "out.nc"))
    assert exc.value.code == 2
    assert not (tmp_path / "out.nc").exists()

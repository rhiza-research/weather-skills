"""Correctness tests for point-value."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def point_value():
    return load_skill("point-value", "point_value").point_value


@pytest.fixture(scope="module")
def difference():
    return load_skill("difference", "difference").difference


def _ramp_grid(tmp_path, name="grid.zarr"):
    """precip = 10*lat + lon on a 1° grid: lat 0..4, lon 10..14."""
    ds = make_gridded(lats=(0.0, 1.0, 2.0, 3.0, 4.0), lons=(10.0, 11.0, 12.0, 13.0, 14.0))
    lat = ds["latitude"].values[:, None]
    lon = ds["longitude"].values[None, :]
    ds["precip"].values[:] = (10 * lat + lon)[None, :, :]
    return write_zarr(ds, tmp_path / name)


def _open(path):
    return xr.open_zarr(path, consolidated=True)


def test_nearest_samples_containing_cell(tmp_path, point_value):
    src = _ramp_grid(tmp_path)
    out = tmp_path / "out.zarr"
    run_skill(
        point_value, "-i", str(src), "-o", str(out),
        "--point", "1.2,11.4,farm", "--point", "2.9,13.6",
    )  # fmt: skip
    ds = _open(out)
    assert ds["precip"].dims == ("station_id", "time")
    assert list(ds["station_id"].values) == ["farm", "P1"]
    np.testing.assert_allclose(ds["precip"].isel(time=0).values, [21.0, 44.0])
    np.testing.assert_allclose(ds["grid_latitude"].values, [1.0, 3.0])
    np.testing.assert_allclose(ds["grid_longitude"].values, [11.0, 14.0])
    np.testing.assert_allclose(ds["latitude"].values, [1.2, 2.9], rtol=1e-5)
    assert ds["station_id"].attrs["cf_role"] == "timeseries_id"
    assert ds.attrs["featureType"] == "timeSeries"
    assert units_equal(ds["precip"].attrs["units"], "mm day-1")


def test_bilinear_interpolates_at_point(tmp_path, point_value):
    src = _ramp_grid(tmp_path)
    out = tmp_path / "out.zarr"
    run_skill(
        point_value, "-i", str(src), "-o", str(out),
        "--point", "1.25,11.5", "--method", "bilinear",
    )  # fmt: skip
    np.testing.assert_allclose(_open(out)["precip"].isel(time=0).values, [24.0], rtol=1e-6)


def test_cell_mean_averages_neighborhood_and_skips_nan(tmp_path, point_value):
    ds = make_gridded(lats=(0.0, 1.0, 2.0), lons=(10.0, 11.0, 12.0), fill=2.0)
    ds["precip"].values[:, 0, 0] = 8.0  # corner pulls a 3x3 mean up
    ds["precip"].values[:, 2, 2] = np.nan  # skipped
    src = write_zarr(ds, tmp_path / "grid.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        point_value, "-i", str(src), "-o", str(out),
        "--point", "1.0,11.0", "--point", "0.0,10.0", "--method", "cell-mean",
    )  # fmt: skip
    res = _open(out)
    # Centre: 8 finite cells, one of them 8 -> (7*2 + 8) / 8.
    # Corner: window clipped to 2x2 -> (8 + 3*2) / 4.
    np.testing.assert_allclose(res["precip"].isel(time=0).values, [22 / 8, 14 / 4])
    assert "area: mean (3x3" in res["precip"].attrs["cell_methods"]


def test_points_zarr_reuses_station_coords_and_joins_provenance(tmp_path, point_value):
    src = _ramp_grid(tmp_path)
    stations = make_point_obs(n_points=2).rename({"point_id": "station_id"})
    stations = stations.assign_coords(
        latitude=("station_id", [1.1, 3.2]),
        longitude=("station_id", [12.2, 10.1]),
        name=("station_id", ["Alpha", "Beta"]),
        country=("station_id", ["KE", "KE"]),
    )
    pts = write_zarr(stations, tmp_path / "stations.zarr")
    out = tmp_path / "out.zarr"
    run_skill(point_value, "-i", str(src), "-o", str(out), "--points", str(pts))
    ds = _open(out)
    assert list(ds["station_id"].values) == ["S0", "S1"]
    assert list(ds["name"].values) == ["Alpha", "Beta"]
    assert list(ds["country"].values) == ["KE", "KE"]
    np.testing.assert_allclose(ds["precip"].isel(time=0).values, [22.0, 40.0])
    entry = load_history(out)[-1]
    assert entry["skill"] == "point-value"
    assert sorted(ref["basename"] for ref in entry["input"]) == ["grid.zarr", "stations.zarr"]


def test_output_differences_against_station_dataset(tmp_path, point_value, difference):
    src = _ramp_grid(tmp_path)
    stations = make_point_obs(n_points=2, n_time=2, fill=5.0).rename({"point_id": "station_id"})
    pts = write_zarr(stations, tmp_path / "stations.zarr")
    sampled = tmp_path / "sampled.zarr"
    run_skill(point_value, "-i", str(src), "-o", str(sampled), "--points", str(pts))
    diff = tmp_path / "diff.zarr"
    run_skill(difference, "-i", str(sampled), "-i", str(pts), "-o", str(diff))
    res = _open(diff)
    assert set(res["precip"].dims) == {"station_id", "time"}
    # Stations at (1,10) and (2,11) -> grid 20 and 31, minus 5.
    np.testing.assert_allclose(res["precip"].isel(time=0).values, [15.0, 26.0])


def test_points_csv(tmp_path, point_value):
    src = _ramp_grid(tmp_path)
    csv_path = tmp_path / "farms.csv"
    csv_path.write_text("id,lat,lon,name\nf1,0.1,10.1,North farm\nf2,4.0,14.0,South farm\n")
    out = tmp_path / "out.zarr"
    run_skill(point_value, "-i", str(src), "-o", str(out), "--points-csv", str(csv_path))
    ds = _open(out)
    assert list(ds["station_id"].values) == ["f1", "f2"]
    assert list(ds["name"].values) == ["North farm", "South farm"]
    np.testing.assert_allclose(ds["precip"].isel(time=0).values, [10.0, 54.0])


def test_forecast_dims_preserved(tmp_path, point_value):
    fc = make_forecast(members=3, lats=(0.0, 1.0), lons=(10.0, 11.0), fill=4.0)
    src = write_zarr(fc, tmp_path / "fc.zarr")
    out = tmp_path / "out.zarr"
    run_skill(point_value, "-i", str(src), "-o", str(out), "--point", "0.4,10.6,site")
    ds = _open(out)
    assert ds["tp"].dims[0] == "station_id"
    assert set(ds["tp"].dims) == {"station_id", "number", "step"}
    assert ds.sizes["number"] == 3
    np.testing.assert_allclose(ds["tp"].values, 4.0)


def test_outside_grid_is_nan(tmp_path, point_value, capsys):
    src = _ramp_grid(tmp_path)
    out = tmp_path / "out.zarr"
    run_skill(
        point_value, "-i", str(src), "-o", str(out),
        "--point", "2.0,12.0,in", "--point", "9.0,12.0,out",
    )  # fmt: skip
    vals = _open(out)["precip"].isel(time=0).values
    assert vals[0] == 32.0 and np.isnan(vals[1])
    assert "1 point(s) set to NaN" in capsys.readouterr().err


def test_max_distance_snaps_to_nearest_valid_cell(tmp_path, point_value):
    ds = make_gridded(lats=(0.0, 1.0, 2.0), lons=(10.0, 11.0, 12.0), fill=np.nan)
    ds["precip"].values[:, 1, 2] = 7.0  # only valid cell: (1, 12)
    src = write_zarr(ds, tmp_path / "grid.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        point_value, "-i", str(src), "-o", str(out),
        "--point", "1.0,11.0,near", "--point=-1.0,10.0,far", "--max-distance", "1.5",
    )  # fmt: skip
    res = _open(out)
    vals = res["precip"].isel(time=0).values
    assert vals[0] == 7.0 and np.isnan(vals[1])
    np.testing.assert_allclose(res["grid_longitude"].values[0], 12.0)
    np.testing.assert_allclose(res["grid_distance"].values[0], 1.0)
    assert np.isnan(res["grid_latitude"].values[1])


def test_descending_latitude_grid(tmp_path, point_value):
    ds = xr.open_zarr(_ramp_grid(tmp_path), consolidated=True).load()
    flipped = write_zarr(ds.isel(latitude=slice(None, None, -1)), tmp_path / "flip.zarr")
    out = tmp_path / "out.zarr"
    run_skill(point_value, "-i", str(flipped), "-o", str(out), "--point", "3.1,11.0")
    np.testing.assert_allclose(_open(out)["precip"].isel(time=0).values, [41.0])


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--point", "1,11", "--points-csv", "x.csv"],
        ["--point", "1"],
        ["--point", "1,11", "--neighborhood", "3"],
        ["--point", "1,11", "--method", "cell-mean", "--neighborhood", "2"],
        ["--point", "1,11,a", "--point", "2,12,a"],
    ],
)
def test_usage_errors(tmp_path, point_value, extra):
    src = _ramp_grid(tmp_path)
    with pytest.raises(SystemExit) as exc:
        run_skill(point_value, "-i", str(src), "-o", str(tmp_path / "o.zarr"), *extra)
    assert exc.value.code == 2

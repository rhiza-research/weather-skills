"""Correctness tests for wind-rose-counts."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_point_obs, run_skill, write_zarr


@pytest.fixture(scope="module")
def rose():
    return load_skill("wind-rose-counts", "wind_rose_counts").wind_rose_counts


def _wind(speeds, directions, units="m s-1"):
    """One station per row of speeds/directions, one time per column."""
    speeds = np.atleast_2d(np.asarray(speeds, dtype=float))
    ds = make_point_obs(n_time=speeds.shape[1], n_points=speeds.shape[0], name="wind_speed")
    ds["wind_speed"].values[:] = speeds
    ds["wind_speed"].attrs = {"units": units}
    ds["wind_from_direction"] = ds["wind_speed"].copy(
        data=np.atleast_2d(np.asarray(directions, dtype=float))
    )
    ds["wind_from_direction"].attrs = {"units": "degree"}
    return ds


def _run(rose, tmp_path, ds, *extra):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(rose, "-i", str(src), "-o", str(out), *extra)
    return xr.open_zarr(out, consolidated=True)


def test_sectors_and_classes(tmp_path, rose):
    # North (both edges of sector 0), east, south-west, and a fast westerly.
    speeds = [1.0, 3.0, 3.5, 5.0, 13.0, 0.0]
    dirs = [349.0, 11.0, 90.0, 225.0, 270.0, np.nan]
    ds = _run(rose, tmp_path, _wind(speeds, dirs))
    assert ds["count"].dims == ("direction", "speed_bin")
    assert ds.sizes == {"direction": 16, "speed_bin": 7}
    count = ds["count"]
    assert float(count.sel(direction=0.0, speed_bin=0.0)) == 1
    assert float(count.sel(direction=0.0, speed_bin=2.0)) == 1
    assert float(count.sel(direction=90.0, speed_bin=2.0)) == 1
    assert float(count.sel(direction=225.0, speed_bin=4.0)) == 1
    assert float(count.sel(direction=270.0, speed_bin=12.0)) == 1
    assert float(count.sum()) == 5
    assert float(ds["calm_count"]) == 1
    assert float(ds["sample_count"]) == 6
    total = float(ds["frequency"].sum()) + float(ds["calm_frequency"])
    assert total == pytest.approx(100.0)
    assert np.isinf(ds["speed_upper"].values[-1])
    assert ds["frequency"].attrs["units"] == "percent"


def test_keep_station(tmp_path, rose):
    speeds = [[3.0, 3.0], [3.0, 0.0]]
    dirs = [[0.0, 180.0], [180.0, np.nan]]
    ds = _run(rose, tmp_path, _wind(speeds, dirs), "--keep", "point_id", "--sectors", "4")
    assert ds["count"].dims == ("point_id", "direction", "speed_bin")
    assert list(ds["direction"].values) == [0.0, 90.0, 180.0, 270.0]
    assert "latitude" in ds.coords
    s0 = ds["count"].sel(point_id="S0").sum("speed_bin")
    assert list(s0.values) == [1, 0, 1, 0]
    assert list(ds["calm_count"].values) == [0, 1]
    s1_south = ds["frequency"].sel(point_id="S1", direction=180.0).sum("speed_bin")
    assert float(s1_south) == 50.0


def test_missing_direction_left_out(tmp_path, rose, capsys):
    ds = _run(rose, tmp_path, _wind([5.0, 5.0], [90.0, np.nan]))
    assert float(ds["sample_count"]) == 1
    assert float(ds["calm_count"]) == 0
    assert "left out 1 sample" in capsys.readouterr().err


def test_custom_edges(tmp_path, rose):
    ds = _run(
        rose, tmp_path, _wind([5.0, 25.0], [0.0, 0.0], units="km h-1"), "--speed-edges", "0,10,20"
    )
    assert list(ds["speed_bin"].values) == [0.0, 10.0, 20.0]
    assert list(ds["count"].sel(direction=0.0).values) == [1, 0, 1]
    assert ds["speed_bin"].attrs["units"] == "km h-1"


@pytest.mark.parametrize(
    "extra",
    [
        ("--speed", "nope"),
        ("--keep", "nope"),
        ("--sectors", "2"),
        ("--speed-edges", "1,2"),
        ("--speed-edges", "0,4,2"),
    ],
)
def test_usage_errors(tmp_path, rose, extra):
    with pytest.raises(SystemExit) as exc:
        _run(rose, tmp_path, _wind([1.0], [0.0]), *extra)
    assert exc.value.code == 2


def test_other_units_need_edges(tmp_path, rose):
    with pytest.raises(SystemExit) as exc:
        _run(rose, tmp_path, _wind([1.0], [0.0], units="knot"))
    assert exc.value.code == 2

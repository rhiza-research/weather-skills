"""Correctness tests for local-extrema."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def local_extrema():
    return load_skill("local-extrema", "local_extrema").local_extrema


def _pressure():
    """Two days of MSLP (hPa): a 1030 high at (10N, 20E); a 990 low that moves 60E -> 50E."""
    lats, lons = np.arange(-30.0, 30.5, 1.0), np.arange(0.0, 80.5, 1.0)
    lat, lon = np.meshgrid(lats, lons, indexing="ij")
    maps = [
        1010
        + 20 * np.exp(-((lat - 10) ** 2 + (lon - 20) ** 2) / 50)
        - 20 * np.exp(-((lat + 10) ** 2 + (lon - low) ** 2) / 50)
        for low in (60, 50)
    ]
    ds = xr.Dataset(
        {"msl": (("time", "latitude", "longitude"), np.stack(maps))},
        coords={
            "time": np.array(["2026-01-01", "2026-01-02"], dtype="datetime64[ns]"),
            "latitude": lats,
            "longitude": lons,
        },
    )
    ds["msl"].attrs.update(units="hPa", standard_name="air_pressure_at_mean_sea_level")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    return ds


def _run(local_extrema, tmp_path, ds, *args):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(local_extrema, "-i", str(src), "-o", str(out), *args)
    return out, xr.open_zarr(out).load()


def test_high_is_one_point_present_on_both_days(tmp_path, local_extrema):
    out, ds = _run(local_extrema, tmp_path, _pressure(), "--kind", "high")
    assert list(ds["station_id"].values) == ["H1"]
    assert (float(ds["latitude"][0]), float(ds["longitude"][0])) == (10.0, 20.0)
    assert ds["msl"].dims == ("station_id", "time")
    assert np.allclose(ds["msl"].values, 1030, atol=0.01)
    assert ds["msl"].attrs["units"] == "hPa" and ds["msl"].attrs["extremum_kind"] == "high"
    assert ds["station_id"].attrs["cf_role"] == "timeseries_id"
    assert load_history(out)[-1]["skill"] == "local-extrema"


def test_moving_low_is_a_point_per_position_nan_on_the_other_day(tmp_path, local_extrema):
    _, ds = _run(local_extrema, tmp_path, _pressure(), "--kind", "low")
    assert sorted(ds["station_id"].values) == ["L1", "L2"]
    by_lon = {float(ds["longitude"][i]): ds["msl"].values[i] for i in range(2)}
    assert np.isclose(by_lon[60.0][0], 990, atol=0.01) and np.isnan(by_lon[60.0][1])
    assert np.isnan(by_lon[50.0][0]) and np.isclose(by_lon[50.0][1], 990, atol=0.01)


def test_flat_field_has_no_centres(tmp_path, local_extrema, capsys):
    flat = make_gridded(n_time=1, lats=tuple(range(10)), lons=tuple(range(10)))
    _, ds = _run(local_extrema, tmp_path, flat, "--kind", "high", "--radius", "2")
    assert ds.sizes["station_id"] == 0
    assert "no high centres found" in capsys.readouterr().err


def test_radius_merges_nearby_centres(tmp_path, local_extrema):
    ds = _pressure().isel(time=[0])
    lat, lon = np.meshgrid(ds["latitude"], ds["longitude"], indexing="ij")
    # A second, weaker high 15 degrees east of the first.
    ds["msl"].values[0] += 15 * np.exp(-((lat - 10) ** 2 + (lon - 35) ** 2) / 20)
    _, near = _run(local_extrema, tmp_path, ds, "--kind", "high")
    _, wide = _run(local_extrema, tmp_path, ds, "--kind", "high", "--radius", "20")
    assert near.sizes["station_id"] == 2 and wide.sizes["station_id"] == 1


def test_needs_variable_when_several(tmp_path, local_extrema):
    ds = _pressure()
    ds["t2m"] = ds["msl"] * 0
    with pytest.raises(SystemExit):
        _run(local_extrema, tmp_path, ds, "--kind", "high")


def _bump(lats, lons, centres, sign=-1, w=50.0):
    """MSLP (hPa) of 1010 plus a 20 hPa bump per (lat, lon) centre, one map per entry."""
    lat, lon = np.meshgrid(lats, lons, indexing="ij")
    maps = []
    for clat, clon in centres:
        dlon = (lon - clon + 180) % 360 - 180  # great-circle-ish distance across the dateline
        maps.append(1010 + sign * 20 * np.exp(-((lat - clat) ** 2 + dlon**2) / w))
    ds = xr.Dataset(
        {"msl": (("time", "latitude", "longitude"), np.stack(maps))},
        coords={
            "time": np.arange(len(maps)) * np.timedelta64(1, "D")
            + np.datetime64("2026-01-01", "ns"),
            "latitude": lats,
            "longitude": lons,
        },
    )
    ds["msl"].attrs["units"] = "hPa"
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    return ds


def test_global_grid_wraps_at_the_dateline(tmp_path, local_extrema):
    ds = _bump(np.arange(-90.0, 90.5, 1.0), np.arange(-180.0, 180.0, 1.0), [(45, 180)])
    _, out = _run(local_extrema, tmp_path, ds, "--kind", "low")
    assert out.sizes["station_id"] == 1
    assert (float(out["latitude"][0]), abs(float(out["longitude"][0]))) == (45.0, 180.0)


def test_bbox_across_the_dateline_is_one_map(tmp_path, local_extrema):
    lats, lons = np.arange(-20.0, 20.5, 0.5), np.arange(160.0, 200.5, 0.5)
    _, low = _run(local_extrema, tmp_path, _bump(lats, lons, [(0, 180)]), "--kind", "low")
    assert low.sizes["station_id"] == 1 and abs(float(low["longitude"][0])) == 180.0
    # A high centred west of the bbox is still rising at its west edge (160E): no centre.
    off = _bump(lats, lons, [(0, 155)], sign=1, w=80)
    _, high = _run(local_extrema, tmp_path, off, "--kind", "high")
    assert high.sizes["station_id"] == 0


def test_no_centre_against_masked_cells(tmp_path, local_extrema):
    # SST rising toward a NaN land mask east of 30E: the warmest water is at the coast.
    ds = _bump(np.arange(-20.0, 20.5, 0.5), np.arange(0.0, 40.5, 0.5), [(0, 0)])
    lon = ds["longitude"].values
    ds["msl"].values[0] = np.where(lon > 30, np.nan, 25 + 0.2 * lon)
    _, out = _run(local_extrema, tmp_path, ds, "--kind", "high", "--radius", "5")
    assert out.sizes["station_id"] == 0


def test_centre_moving_one_cell_keeps_its_id(tmp_path, local_extrema):
    lats, lons = np.arange(-20.0, 20.5, 0.5), np.arange(0.0, 40.5, 0.5)
    ds = _bump(lats, lons, [(0, 20), (0, 20.5), (0, 25)])
    _, out = _run(local_extrema, tmp_path, ds, "--kind", "low")
    # Days 1 and 2 are one point at the first cell; day 3 is 5 degrees on, a new point.
    assert out.sizes["station_id"] == 2
    first = int(np.argmin(np.abs(out["longitude"].values - 20)))
    assert float(out["longitude"][first]) == 20.0
    assert np.isfinite(out["msl"].values[first, :2]).all() and np.isnan(out["msl"].values[first, 2])


@pytest.mark.parametrize("radius", ["0", "-1", "nan", "inf"])
def test_radius_must_be_positive_and_finite(tmp_path, local_extrema, radius):
    with pytest.raises(SystemExit):
        _run(local_extrema, tmp_path, _pressure(), "--kind", "high", "--radius", radius)

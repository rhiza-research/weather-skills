"""Correctness tests for wind-speed-direction."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def wind():
    return load_skill("wind-speed-direction", "wind_speed_direction").wind_speed_direction


def _run(wind, tmp_path, ds, u="u10", v="v10", *extra):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(wind, "-i", str(src), "-o", str(out), "--u", u, "--v", v, *extra)
    return xr.open_zarr(out, consolidated=True), out


def _usage_error(wind, tmp_path, ds, *extra):
    with pytest.raises(SystemExit) as exc:
        _run(wind, tmp_path, ds, "u10", "v10", *extra)
    assert exc.value.code == 2


def _uv_gridded(u_vals, v_vals):
    """One time step, one latitude, one longitude per (u, v) pair."""
    n = len(u_vals)
    ds = make_gridded(n_time=1, lats=(1.0,), lons=tuple(10.0 + i for i in range(n)), name="u10")
    ds["u10"].values[:] = np.array(u_vals, dtype=float)
    ds["v10"] = ds["u10"].copy(data=np.array(v_vals, dtype=float).reshape(1, 1, n))
    for name in ("u10", "v10"):
        ds[name].attrs = {"units": "m s-1"}
    return ds


# (u, v, speed, from, to). A northerly blows from the north (v < 0).
CASES = [
    (0.0, -5.0, 5.0, 0.0, 180.0),  # northerly
    (0.0, 5.0, 5.0, 180.0, 0.0),  # southerly
    (-5.0, 0.0, 5.0, 90.0, 270.0),  # easterly
    (5.0, 0.0, 5.0, 270.0, 90.0),  # westerly
    (3.0, 3.0, np.hypot(3.0, 3.0), 225.0, 45.0),  # south-westerly
    (-3.0, -4.0, 5.0, np.degrees(np.arctan2(3.0, 4.0)), 180.0 + np.degrees(np.arctan2(3.0, 4.0))),
    (0.0, 0.0, 0.0, np.nan, np.nan),  # calm
]


def test_known_pairs(tmp_path, wind):
    u, v, speed, from_dir, to_dir = (list(c) for c in zip(*CASES, strict=True))
    ds, out = _run(wind, tmp_path, _uv_gridded(u, v))
    assert set(ds.data_vars) == {"wind_speed", "wind_from_direction", "wind_to_direction"}
    np.testing.assert_allclose(ds["wind_speed"].values.ravel(), speed)
    np.testing.assert_allclose(ds["wind_from_direction"].values.ravel(), from_dir, atol=1e-9)
    np.testing.assert_allclose(ds["wind_to_direction"].values.ravel(), to_dir, atol=1e-9)
    assert ds["wind_speed"].attrs["units"] == "m s-1"
    assert ds["wind_speed"].attrs["standard_name"] == "wind_speed"
    assert ds["wind_from_direction"].attrs["standard_name"] == "wind_from_direction"
    assert ds["wind_to_direction"].attrs["standard_name"] == "wind_to_direction"
    assert ds["wind_to_direction"].attrs["units"] == "degree"
    assert load_history(out)[-1]["skill"] == "wind-speed-direction"


def test_directions_stay_below_360(tmp_path, wind):
    # A tiny negative u just west of north rounds to 360 unless wrapped to 0.
    ds, _ = _run(wind, tmp_path, _uv_gridded([-1e-14, 1e-14], [5.0, -5.0]))
    for name in ("wind_from_direction", "wind_to_direction"):
        values = ds[name].values
        assert ((values >= 0) & (values < 360)).all()


def test_station_dataset(tmp_path, wind):
    ds = make_point_obs(n_time=2, n_points=2, name="u10", fill=0.0)
    ds["u10"].values[:] = [[3.0, 0.0], [0.0, -4.0]]
    ds["v10"] = ds["u10"].copy(data=np.array([[4.0, -2.0], [0.0, 0.0]]))
    for name in ("u10", "v10"):
        ds[name].attrs = {"units": "m s-1"}
    out, _ = _run(wind, tmp_path, ds)
    assert out["wind_speed"].dims == ("point_id", "time")
    assert "latitude" in out.coords
    np.testing.assert_allclose(out["wind_speed"].values, [[5.0, 2.0], [0.0, 4.0]])
    np.testing.assert_allclose(
        out["wind_from_direction"].values,
        [[180.0 + np.degrees(np.arctan2(3.0, 4.0)), 0.0], [np.nan, 90.0]],
    )


def test_pressure_level_grid(tmp_path, wind):
    ds = make_forecast(n_step=2, name="u", fill=10.0, members=2)
    ds = ds.expand_dims(pressure_level=[850.0, 500.0], axis=1)
    ds["pressure_level"].attrs.update(units="hPa", standard_name="air_pressure")
    ds["v"] = ds["u"] * 0.0 - 10.0
    for name in ("u", "v"):
        ds[name].attrs = {"units": "knot"}
    out, _ = _run(wind, tmp_path, ds, u="u", v="v")
    assert out["wind_speed"].dims == ds["u"].dims
    np.testing.assert_allclose(out["wind_speed"].values, np.hypot(10.0, 10.0))
    np.testing.assert_allclose(out["wind_from_direction"].values, 315.0)
    np.testing.assert_allclose(out["wind_to_direction"].values, 135.0)
    assert units_equal(out["wind_speed"].attrs["units"], "knot")


def test_unit_mismatch_is_refused(tmp_path, wind):
    ds = _uv_gridded([1.0], [1.0])
    ds["v10"].attrs["units"] = "km h-1"
    _usage_error(wind, tmp_path, ds)


def test_different_dims_are_refused(tmp_path, wind):
    ds = _uv_gridded([1.0, 2.0], [1.0, 2.0])
    ds["v10"] = ds["v10"].rename(longitude="lon_v")
    _usage_error(wind, tmp_path, ds)


def test_missing_variable_is_refused(tmp_path, wind):
    _usage_error(wind, tmp_path, _uv_gridded([1.0], [1.0]).drop_vars("v10"))


def test_near_zero_is_calm_by_default(tmp_path, wind):
    ds, _ = _run(wind, tmp_path, _uv_gridded([1e-9, 0.3], [-1e-9, 0.4]))
    np.testing.assert_allclose(ds["wind_speed"].values.ravel(), [np.hypot(1e-9, 1e-9), 0.5])
    assert np.isnan(ds["wind_from_direction"].values.ravel()[0])
    assert np.isfinite(ds["wind_from_direction"].values.ravel()[1])


def test_calm_speed_threshold(tmp_path, wind):
    ds, _ = _run(
        wind, tmp_path, _uv_gridded([0.3, 3.0], [0.4, 4.0]), "u10", "v10", "--calm-speed", "0.5"
    )
    # Speed 0.5 is at the threshold, so calm; speed is kept as computed.
    np.testing.assert_allclose(ds["wind_speed"].values.ravel(), [0.5, 5.0])
    assert np.isnan(ds["wind_to_direction"].values.ravel()[0])
    assert np.isfinite(ds["wind_to_direction"].values.ravel()[1])


def test_negative_calm_speed_is_refused(tmp_path, wind):
    _usage_error(wind, tmp_path, _uv_gridded([1.0], [1.0]), "--calm-speed", "-1")


def _arrows(out, k=0):
    """Arrow k's seven vertices as (x, y) arrays."""
    x = out["arrow_x"].values.reshape(-1, 7)[k]
    y = out["arrow_y"].values.reshape(-1, 7)[k]
    return x, y


def test_arrows_are_quiver_segments(tmp_path, wind):
    # A 5 m s-1 westerly at 10°E, a 10 m s-1 southerly at 11°E.
    out, _ = _run(
        wind, tmp_path, _uv_gridded([5.0, 0.0], [0.0, 10.0]), "u10", "v10", "--arrows", "0.1"
    )
    assert out["arrow_x"].dims == ("time", "arrow_vertex") and out["arrow_x"].shape == (1, 14)
    x, y = _arrows(out, 0)
    # Tail at the cell, tip 0.5° east, NaN gaps after the shaft and after the head.
    np.testing.assert_allclose([x[0], x[1], y[0], y[1]], [10.0, 10.5, 1.0, 1.0])
    assert np.isnan(x[2]) and np.isnan(x[6])
    # Head barbs end 0.3 of the shaft back from the tip, 20° either side of it.
    np.testing.assert_allclose(x[4], 10.5)
    np.testing.assert_allclose([x[3], x[5]], 10.5 - 0.15 * np.cos(np.radians(20)), rtol=1e-6)
    np.testing.assert_allclose(
        sorted([y[3], y[5]]),
        [1 - 0.15 * np.sin(np.radians(20)), 1 + 0.15 * np.sin(np.radians(20))],
        rtol=1e-6,
    )
    x, y = _arrows(out, 1)
    np.testing.assert_allclose([x[0], x[1], y[0], y[1]], [11.0, 11.0, 1.0, 2.0])
    assert out["arrow_x"].attrs["arrow_scale"] == "0.1 degree per m s-1"


def test_arrows_every_nth_cell_and_calm_is_empty(tmp_path, wind):
    out, _ = _run(
        wind,
        tmp_path,
        _uv_gridded([5.0, 0.0, 0.0, 2.0], [0.0, 0.0, 1.0, 0.0]),
        "u10",
        "v10",
        "--arrows",
        "0.1",
        "--every",
        "2",
    )
    assert out["arrow_x"].shape == (1, 14)  # cells 0 and 2 of 4
    x, _ = _arrows(out, 0)
    assert x[0] == 10.0
    calm, _ = _run(wind, tmp_path / "c", _uv_gridded([0.0], [0.0]), "u10", "v10", "--arrows", "0.1")
    assert np.isnan(calm["arrow_x"].values).all()


def test_arrows_at_stations(tmp_path, wind):
    ds = make_point_obs(n_time=2, n_points=3, name="u10")
    ds["u10"].values[:] = 4.0
    ds["v10"] = ds["u10"] * 0
    for name in ("u10", "v10"):
        ds[name].attrs = {"units": "m s-1"}
    out, _ = _run(wind, tmp_path, ds, "u10", "v10", "--arrows", "0.05")
    assert (
        set(out["arrow_x"].dims) == {"time", "arrow_vertex"}
        and out["arrow_x"].sizes["arrow_vertex"] == 21
    )
    x = out["arrow_x"].isel(time=0).values.reshape(-1, 7)
    np.testing.assert_allclose(x[:, 1] - x[:, 0], 0.2)


def test_bad_arrow_options_are_refused(tmp_path, wind):
    _usage_error(wind, tmp_path, _uv_gridded([1.0], [1.0]), "--arrows", "0")
    _usage_error(wind, tmp_path / "e", _uv_gridded([1.0], [1.0]), "--arrows", "0.1", "--every", "0")

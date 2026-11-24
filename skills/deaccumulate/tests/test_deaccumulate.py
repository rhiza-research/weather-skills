"""Correctness tests for deaccumulate."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def deaccumulate():
    return load_skill("deaccumulate", "deaccumulate").deaccumulate


def test_deaccumulate_cumulative_tp(tmp_path, deaccumulate):
    ds = make_forecast(n_step=3, lats=(1.0,), lons=(10.0,), fill=0.0)
    data = np.array([[[1.0]], [[3.0]], [[6.0]]])
    ds = ds.assign(tp=(("step", "latitude", "longitude"), data))
    ds["tp"].attrs.update(units="mm", long_name="Total precipitation")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(deaccumulate, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    result = xr.open_zarr(out, consolidated=True)
    assert result.sizes["step"] == 2
    np.testing.assert_array_equal(
        np.asarray(result["step"].values).astype("timedelta64[D]"),
        np.array([0, 1], dtype="timedelta64[D]"),
    )
    vals = result["tp"].values.flatten()
    assert vals[0] == pytest.approx(2.0)
    assert vals[1] == pytest.approx(3.0)
    assert result["tp"].attrs.get("data_interval") == "1 day"
    assert "step_bounds" not in result.variables
    assert load_history(out)[-1]["skill"] == "deaccumulate"


def test_deaccumulate_needs_two_steps(tmp_path, deaccumulate):
    ds = make_forecast(n_step=1)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(deaccumulate, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def _cumulative(tmp_path, steps_days, **variables):
    """Write a 1x1 forecast with the given (values, attrs) per variable."""
    ds = make_forecast(n_step=len(steps_days), lats=(1.0,), lons=(10.0,), fill=0.0)
    ds = ds.drop_vars("tp").assign_coords(
        step=np.array(steps_days, dtype="timedelta64[D]").astype("timedelta64[ns]")
    )
    for name, (values, attrs) in variables.items():
        ds[name] = (("step", "latitude", "longitude"), np.reshape(values, (-1, 1, 1)), attrs)
    return write_zarr(ds, tmp_path / "in.zarr")


def test_irregular_steps_divide_by_each_interval(tmp_path, deaccumulate):
    # ECMWF S2S-like irregular leads in kg m-2: 14 over 7 days, 6 over 3 days.
    src = _cumulative(tmp_path, [0, 7, 10], tp=([0.0, 14.0, 20.0], {"units": "kg m-2"}))
    out = tmp_path / "out.zarr"

    run_skill(deaccumulate, "-i", str(src), "-o", str(out))

    result = xr.open_zarr(out, consolidated=True)
    np.testing.assert_allclose(result["tp"].values.ravel(), [2.0, 2.0])
    assert result["tp"].attrs["units"] == "mm day-1"
    assert result["tp"].attrs.get("data_interval") is None
    bounds = result["step_bounds"].values.astype("timedelta64[D]").astype(int)
    np.testing.assert_array_equal(bounds, [[0, 7], [7, 10]])


def test_nan_step_is_not_filled(tmp_path, deaccumulate):
    src = _cumulative(tmp_path, [0, 1, 2, 3], tp=([0.0, 2.0, np.nan, 9.0], {"units": "mm"}))
    out = tmp_path / "out.zarr"

    run_skill(deaccumulate, "-i", str(src), "-o", str(out))

    vals = xr.open_zarr(out, consolidated=True)["tp"].values.ravel()
    assert vals[0] == pytest.approx(2.0)
    assert np.isnan(vals[1:]).all()


def test_refuses_rate_input(tmp_path, deaccumulate):
    src = _cumulative(tmp_path, [0, 1, 2], tp=([1.0, 2.0, 3.0], {"units": "mm day-1"}))
    with pytest.raises(SystemExit) as exc:
        run_skill(deaccumulate, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_multiple_vars_require_variable(tmp_path, deaccumulate):
    src = _cumulative(
        tmp_path,
        [0, 1, 2],
        tp=([1.0, 3.0, 6.0], {"units": "mm"}),
        t2m=([20.0, 21.0, 22.0], {"units": "degree_Celsius"}),
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(deaccumulate, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2

    out = tmp_path / "out.zarr"
    run_skill(deaccumulate, "-i", str(src), "-o", str(out), "--variable", "tp")
    result = xr.open_zarr(out, consolidated=True)
    np.testing.assert_allclose(result["tp"].values.ravel(), [2.0, 3.0])
    # The companion is not differenced: it keeps its left-edge values.
    np.testing.assert_allclose(result["t2m"].values.ravel(), [20.0, 21.0])


@pytest.mark.parametrize(
    ("name", "values", "units"),
    [("e", [0.0, -0.002, -0.005], "m"), ("ttr", [0.0, -2e7, -4e7], "J m-2")],
)
def test_refuses_negative_accumulation(tmp_path, deaccumulate, name, values, units):
    # Clipping these increments at zero would silently write all zeros.
    src = _cumulative(tmp_path, [0, 1, 2], **{name: (values, {"units": units})})
    with pytest.raises(SystemExit) as exc:
        run_skill(deaccumulate, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_warns_without_units_or_standard_name(tmp_path, deaccumulate, capsys):
    src = _cumulative(tmp_path, [0, 1, 2], foo=([1.0, 3.0, 6.0], {}))
    out = tmp_path / "out.zarr"

    run_skill(deaccumulate, "-i", str(src), "-o", str(out))

    assert "no units or standard_name" in capsys.readouterr().err
    np.testing.assert_allclose(xr.open_zarr(out)["foo"].values.ravel(), [2.0, 3.0])

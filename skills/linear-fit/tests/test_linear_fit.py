"""Correctness tests for linear-fit."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from scipy import stats
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal

FORECAST = [37.0, 40.0, 47.0, 52.0, 57.0, 65.0, 77.0, 89.0, 105.0, 139.0, 140.0]
OBSERVED = [15.0, 28.0, 41.0, 44.0, 32.0, 76.0, 45.0, 63.0, 94.0, 187.0, np.nan]


@pytest.fixture(scope="module")
def linear_fit():
    return load_skill("linear-fit", "linear_fit").linear_fit


def _years(forecast=FORECAST, observed=OBSERVED):
    years = np.arange(2015, 2015 + len(forecast))
    ds = xr.Dataset(
        {"tp": ("year", forecast), "precip": ("year", observed)},
        coords={"year": years},
    )
    ds["tp"].attrs.update(units="mm")
    ds["precip"].attrs.update(units="mm")
    return ds


def _run(linear_fit, tmp_path, ds, *extra):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        linear_fit,
        "-i",
        str(src),
        "-o",
        str(out),
        "--x",
        "tp",
        "--y",
        "precip",
        "--dim",
        "year",
        *extra,
    )
    return out


def test_matches_scipy_and_predicts_missing_year(tmp_path, linear_fit):
    out = _run(linear_fit, tmp_path, _years())
    ds = xr.open_zarr(out, consolidated=True)

    x = np.array(FORECAST[:-1])
    y = np.array(OBSERVED[:-1])
    ref = stats.linregress(x, y)
    assert float(ds["slope"]) == pytest.approx(ref.slope)
    assert float(ds["intercept"]) == pytest.approx(ref.intercept)
    assert float(ds["r2"]) == pytest.approx(ref.rvalue**2)
    assert int(ds["n"]) == 10

    # 2025 has no observation: it is not fitted but gets a prediction and interval.
    x0 = FORECAST[-1]
    pred = ref.intercept + ref.slope * x0
    s = np.sqrt(np.sum((y - (ref.intercept + ref.slope * x)) ** 2) / 8)
    half = (
        stats.t.ppf(0.9, 8)
        * s
        * np.sqrt(1 + 1 / 10 + (x0 - x.mean()) ** 2 / ((x - x.mean()) ** 2).sum())
    )
    last = ds.sel(year=2025)
    assert float(last["fit"]) == pytest.approx(pred)
    assert float(last["fit_lower"]) == pytest.approx(pred - half)
    assert float(last["fit_upper"]) == pytest.approx(pred + half)
    assert np.isnan(float(last["precip"]))
    assert float(ds["residual_std"]) == pytest.approx(s)
    assert load_history(out)[-1]["skill"] == "linear-fit"


def test_units(tmp_path, linear_fit):
    ds = _years()
    ds["tp"].attrs["units"] = "mm day-1"
    out = _run(linear_fit, tmp_path, ds)
    res = xr.open_zarr(out, consolidated=True)

    assert units_equal(res["fit"].attrs["units"], "mm")
    assert units_equal(res["fit_upper"].attrs["units"], "mm")
    assert units_equal(res["slope"].attrs["units"], "day")
    assert res["fit_lower"].attrs["prediction_interval"] == pytest.approx(0.8)


def test_wider_interval_is_wider(tmp_path, linear_fit):
    narrow = xr.open_zarr(_run(linear_fit, tmp_path / "a", _years()), consolidated=True)
    wide = xr.open_zarr(
        _run(linear_fit, tmp_path / "b", _years(), "--interval", "0.95"), consolidated=True
    )
    width = lambda d: (d["fit_upper"] - d["fit_lower"]).values  # noqa: E731
    assert np.all(width(wide) > width(narrow))
    np.testing.assert_allclose(wide["fit"].values, narrow["fit"].values)


def test_fits_each_cell(tmp_path, linear_fit):
    x = np.array([1.0, 2.0, 3.0, 4.0])
    # Cell 0: y = 2x + 1 exactly; cell 1: y = -x.
    y = np.stack([2 * x + 1, -x], axis=1)
    ds = xr.Dataset(
        {"tp": ("year", x), "precip": (("year", "station_id"), y)},
        coords={"year": [2020, 2021, 2022, 2023], "station_id": ["a", "b"]},
    )
    ds["tp"].attrs.update(units="mm")
    ds["precip"].attrs.update(units="mm")
    res = xr.open_zarr(_run(linear_fit, tmp_path, ds), consolidated=True)

    np.testing.assert_allclose(res["slope"].values, [2.0, -1.0])
    np.testing.assert_allclose(res["intercept"].values, [1.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(res["r2"].values, [1.0, 1.0])


def test_too_few_samples_is_nan(tmp_path, linear_fit):
    ds = _years(forecast=[10.0, 20.0, 30.0], observed=[5.0, 9.0, np.nan])
    res = xr.open_zarr(_run(linear_fit, tmp_path, ds), consolidated=True)

    assert float(res["slope"]) == pytest.approx(0.4)
    assert int(res["n"]) == 2
    assert np.isnan(float(res["residual_std"]))
    assert np.all(np.isnan(res["fit_upper"].values))


def test_rejects_bad_args(tmp_path, linear_fit):
    ds = _years()
    src = write_zarr(ds, tmp_path / "in.zarr")
    base = ["-i", str(src), "-o", str(tmp_path / "out.zarr")]
    with pytest.raises(SystemExit) as exc:
        run_skill(linear_fit, *base, "--x", "tp", "--y", "nope", "--dim", "year")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        run_skill(linear_fit, *base, "--x", "tp", "--y", "precip", "--dim", "time")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        run_skill(
            linear_fit, *base, "--x", "tp", "--y", "precip", "--dim", "year", "--interval", "80"
        )
    assert exc.value.code == 2

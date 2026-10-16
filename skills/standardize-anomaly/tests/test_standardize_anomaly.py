"""Correctness tests for standardize-anomaly."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def standardize_anomaly():
    return load_skill("standardize-anomaly", "standardize_anomaly").standardize_anomaly


def _ds(variables, lats=(1.0, 2.0), times=("2026-03-01",)):
    return xr.Dataset(
        {
            name: (
                ("time", "latitude", "longitude"),
                np.full((len(times), len(lats), 1), value),
                {"units": u},
            )
            for name, (value, u) in variables.items()
        },
        coords={
            "time": np.array(times, dtype="datetime64[ns]"),
            "latitude": list(lats),
            "longitude": [10.0],
        },
    )


def _run(tmp_path, fn, field, clim, var):
    pf = write_zarr(field, tmp_path / "field.zarr")
    pc = write_zarr(clim, tmp_path / "clim.zarr")
    out = tmp_path / "out.zarr"
    run_skill(fn, "-i", str(pf), "--climatology", str(pc), "-v", var, "-o", str(out))
    return out


@pytest.mark.parametrize(
    "field, clim, var",
    [
        (
            _ds({"precip": (6.0, "mm day-1")}),
            _ds({"precip_avg": (4.0, "mm day-1"), "precip_std": (1.0, "mm day-1")}),
            "precip",
        ),
        (  # tiny SI magnitudes: no fixed epsilon may swamp the spread
            _ds({"precip": (6 / 86400, "kg m-2 s-1")}),
            _ds({"precip_avg": (4 / 86400, "kg m-2 s-1"), "precip_std": (1 / 86400, "kg m-2 s-1")}),
            "precip",
        ),
        (  # a deg C std is a spread (1 K), not an absolute 274.15 K
            _ds({"t2m": (27.0, "degree_Celsius")}),
            _ds({"t2m_avg": (25.0, "degree_Celsius"), "t2m_std": (1.0, "degree_Celsius")}),
            "t2m",
        ),
        (
            _ds({"t2m": (27.0, "degree_Celsius")}),
            _ds({"t2m_avg": (298.15, "K"), "t2m_std": (1.0, "K")}),
            "t2m",
        ),
        (  # a std stamped as a temperature difference is a spread too
            _ds({"t2m": (27.0, "degree_Celsius")}),
            _ds({"t2m_avg": (298.15, "K"), "t2m_std": (1.0, "delta_degC")}),
            "t2m",
        ),
    ],
)
def test_z_score_is_two_whatever_the_units(tmp_path, standardize_anomaly, field, clim, var):
    out = _run(tmp_path, standardize_anomaly, field, clim, var)
    ds = xr.open_zarr(out, consolidated=True)
    assert ds[f"{var}_anomaly"].values == pytest.approx(2.0)
    assert ds[f"{var}_anomaly"].attrs["units"] == "1"
    assert load_history(out)[-1]["skill"] == "standardize-anomaly"


def test_zero_spread_is_nan(tmp_path, standardize_anomaly):
    out = _run(
        tmp_path,
        standardize_anomaly,
        _ds({"precip": (6.0, "mm day-1")}),
        _ds({"precip_avg": (0.0, "mm day-1"), "precip_std": (0.0, "mm day-1")}),
        "precip",
    )
    assert xr.open_zarr(out, consolidated=True)["precip_anomaly"].isnull().all()


@pytest.mark.parametrize(
    "clim",
    [
        _ds({"precip_avg": (4.0, "mm day-1"), "precip_std": (1.0, "mm day-1")}, lats=(1.05, 2.05)),
        _ds({"precip_avg": (4.0, "K"), "precip_std": (1.0, "K")}),
        _ds({"precip_avg": (4.0, "mm day-1")}),
    ],
    ids=["no-overlap", "incompatible-units", "missing-std"],
)
def test_refuses(tmp_path, standardize_anomaly, clim):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, standardize_anomaly, _ds({"precip": (6.0, "mm day-1")}), clim, "precip")
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


_CLIM = {"precip_avg": (4.0, "mm day-1"), "precip_std": (1.0, "mm day-1")}
_DAYS = ("2026-03-01", "2026-03-02", "2026-03-03")
_FORECAST = (
    _ds({"precip": (6.0, "mm day-1")})
    .expand_dims(step=np.array([0, 1, 2], dtype="timedelta64[D]").astype("timedelta64[ns]"))
    .transpose("time", "step", ...)
)


@pytest.mark.parametrize(
    "field, clim",
    [
        # Inner join used to silently keep only the 00:00 hours / covered days.
        (
            _ds({"precip": (6.0, "mm day-1")}, times=("2026-03-01T00", "2026-03-01T06")),
            _ds(_CLIM),
        ),
        (_ds({"precip": (6.0, "mm day-1")}, times=_DAYS), _ds(_CLIM)),
        (_ds({"precip": (6.0, "mm day-1")}, lats=(1.0, 2.0, 3.0)), _ds(_CLIM)),
        # Used to score every lead against the init day's climatology.
        (_FORECAST, _ds(_CLIM, times=_DAYS)),
        # Used to broadcast a timeless field across every climatology day.
        (_ds({"precip": (6.0, "mm day-1")}).isel(time=0, drop=True), _ds(_CLIM, times=_DAYS)),
    ],
    ids=["sub-daily", "partial-window", "partial-grid", "forecast-lead", "timeless-field"],
)
def test_refuses_misaligned(tmp_path, standardize_anomaly, field, clim):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, standardize_anomaly, field, clim, "precip")
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_extra_climatology_days_are_dropped(tmp_path, standardize_anomaly):
    out = _run(
        tmp_path,
        standardize_anomaly,
        _ds({"precip": (6.0, "mm day-1")}),
        _ds(_CLIM, times=_DAYS),
        "precip",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 1
    assert ds["precip_anomaly"].values == pytest.approx(2.0)


def test_nan_propagates(tmp_path, standardize_anomaly):
    field = _ds({"precip": (6.0, "mm day-1")})
    field["precip"][0, 0, 0] = np.nan
    out = _run(tmp_path, standardize_anomaly, field, _ds(_CLIM), "precip")
    values = xr.open_zarr(out, consolidated=True)["precip_anomaly"].values.ravel()
    assert np.isnan(values[0]) and values[1] == pytest.approx(2.0)

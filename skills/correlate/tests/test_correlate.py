"""Correctness tests for correlate."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def correlate_fn():
    return load_skill("correlate", "correlate").correlate


def _index(values, name="nino34"):
    """A 1-D time series such as a climate index."""
    times = np.arange(
        np.datetime64("2026-01-01"), np.datetime64("2026-01-01") + np.timedelta64(len(values), "D")
    )
    ds = xr.Dataset(
        {name: ("time", np.asarray(values, dtype=float))},
        coords={"time": times.astype("datetime64[ns]")},
    )
    ds[name].attrs["units"] = "delta_degC"
    return ds


def _run(tmp_path, fn, a, b, *args):
    pa, pb = write_zarr(a, tmp_path / "a.zarr"), write_zarr(b, tmp_path / "b.zarr")
    out = tmp_path / "r.zarr"
    run_skill(fn, "-i", str(pa), "-i", str(pb), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True)


def test_index_against_grid(tmp_path, correlate_fn, capsys):
    """An index broadcasts over the grid; each cell gets its own r."""
    x = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    grid = make_gridded(n_time=5, lats=(1.0, 2.0), lons=(10.0, 11.0))
    grid["precip"].values[:] = x[:, None, None]
    grid["precip"].values[:, 0, 1] = -x
    grid["precip"].values[:, 1, 0] = x**3
    ds = _run(tmp_path, correlate_fn, _index(x), grid)
    r = ds["correlation"].values
    assert r[0, 0] == pytest.approx(1.0)
    assert r[0, 1] == pytest.approx(-1.0)
    assert 0.9 < r[1, 0] < 1.0
    assert (ds["n_pairs"].values == 5).all()
    assert ds["p_value"].values[0, 0] < 1e-3
    assert "mean r" in capsys.readouterr().out
    assert load_history(tmp_path / "r.zarr")[-1]["skill"] == "correlate"

    spear = _run(tmp_path, correlate_fn, _index(x), grid, "--method", "spearman")
    assert spear["correlation"].values[1, 0] == pytest.approx(1.0)


def test_nan_pairs_are_dropped(tmp_path, correlate_fn):
    a = _index([1.0, 2.0, np.nan, 4.0, 5.0], name="x")
    b = _index([2.0, 4.0, 100.0, 8.0, np.nan], name="y")
    ds = _run(tmp_path, correlate_fn, a, b, "--method", "spearman")
    assert int(ds["n_pairs"]) == 3
    assert float(ds["correlation"]) == pytest.approx(1.0)


def test_too_few_pairs_is_nan(tmp_path, correlate_fn):
    ds = _run(tmp_path, correlate_fn, _index([1.0, 2.0], "x"), _index([2.0, 1.0], "y"))
    assert np.isnan(float(ds["correlation"]))


def test_pattern_correlation_over_space(tmp_path, correlate_fn):
    a = make_gridded(n_time=2)
    a["precip"].values[:] = np.arange(12).reshape(3, 4)
    b = a.copy(deep=True)
    b["precip"].values[1] *= -1
    ds = _run(tmp_path, correlate_fn, a, b, "--dim", "latitude", "--dim", "longitude")
    assert ds["correlation"].dims == ("time",)
    assert ds["correlation"].values[0] == pytest.approx(1.0)
    assert ds["correlation"].values[1] == pytest.approx(-1.0)


def test_members_and_missing_dim_are_refused(tmp_path, correlate_fn, capsys):
    grid = make_gridded(n_time=3).expand_dims(number=2)
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, correlate_fn, _index([1.0, 2.0, 3.0]), grid)
    assert exc.value.code == 2
    assert "ensemble mean" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path, correlate_fn, _index([1.0, 2.0, 3.0]), make_gridded(n_time=3), "--dim", "step"
        )
    assert exc.value.code == 2

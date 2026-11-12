"""Correctness tests for clip-values."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def clip_values():
    return load_skill("clip-values", "clip_values").clip_values


def _input():
    ds = make_gridded()
    values = np.linspace(-5.0, 50.0, ds["precip"].size).reshape(ds["precip"].shape)
    values.flat[3] = np.nan
    ds["precip"].values = values
    return ds


def _run(clip_values, tmp_path, ds, *args):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(clip_values, "-i", str(src), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True), out


def test_clip_min_and_max(tmp_path, clip_values):
    ds_in = _input()
    ds, out = _run(clip_values, tmp_path, ds_in, "--min", "0", "--max", "30")
    expected = np.clip(ds_in["precip"].values, 0, 30)
    np.testing.assert_array_equal(ds["precip"].values, expected)
    assert np.isnan(ds["precip"].values.flat[3])
    assert ds["precip"].attrs["units"] == ds_in["precip"].attrs["units"]
    assert load_history(out)[-1]["skill"] == "clip-values"


def test_clip_min_only(tmp_path, clip_values):
    ds, _ = _run(clip_values, tmp_path, _input(), "--variable", "precip", "--min", "0")
    assert float(ds["precip"].min()) == 0.0
    assert float(ds["precip"].max()) == 50.0


@pytest.mark.parametrize(
    "args",
    [[], ["--min", "5", "--max", "1"], ["--variable", "nope", "--min", "0"]],
)
def test_bad_arguments_are_refused(tmp_path, clip_values, args):
    with pytest.raises(SystemExit) as exc:
        _run(clip_values, tmp_path, _input(), *args)
    assert exc.value.code == 2

"""Correctness tests for inspect-zarr."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def inspect_zarr():
    return load_skill("inspect-zarr", "inspect_zarr").inspect_zarr


def test_human_prints_dims_and_coord_values(tmp_path, inspect_zarr, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")

    run_skill(inspect_zarr, "-i", str(src))

    out = capsys.readouterr().out
    assert "time: 2" in out
    assert "latitude: 3" in out
    assert "longitude: 4" in out
    assert "1, 2, 3" in out
    assert "10, 11, 12, 13" in out
    assert "precip" in out
    assert "mm d-1" in out
    assert "finite 24/24" in out
    assert "min 1" in out
    assert "sample:" in out


def test_json_format(tmp_path, inspect_zarr, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")

    run_skill(inspect_zarr, "-i", str(src), "--format", "json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["dims"]["latitude"] == 3
    lats = next(c for c in payload["coords"] if c["name"] == "latitude")
    assert lats["values"] == [1.0, 2.0, 3.0]
    assert lats["truncated"] is False
    precip = next(v for v in payload["data_vars"] if v["name"] == "precip")
    assert precip["dims"] == ["time", "latitude", "longitude"]
    assert precip["n"] == 24
    assert precip["n_finite"] == 24
    assert precip["n_nan"] == 0
    assert precip["min"] == 1.0
    assert precip["max"] == 1.0
    assert precip["mean"] == 1.0
    assert precip["sample_truncated"] is False
    assert precip["sample"] == [1.0] * 24


def test_max_values_truncates(tmp_path, inspect_zarr, capsys):
    src = write_zarr(
        make_gridded(lons=tuple(float(i) for i in range(10, 20))),
        tmp_path / "in.zarr",
    )

    run_skill(inspect_zarr, "-i", str(src), "--max-values", "2")

    out = capsys.readouterr().out
    assert "…" in out
    assert "(10 values)" in out


def test_forecast_scalar_init_time(tmp_path, inspect_zarr, capsys):
    src = write_zarr(make_forecast(), tmp_path / "in.zarr")

    run_skill(inspect_zarr, "-i", str(src), "--format", "json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["dims"]["step"] == 3
    time = next(c for c in payload["coords"] if c["name"] == "time")
    assert time["size"] == 1
    assert "2026-01-01" in str(time["values"][0])


def test_accepts_precip_totals(tmp_path, inspect_zarr, capsys):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")

    run_skill(inspect_zarr, "-i", str(src))

    assert "precip" in capsys.readouterr().out


def test_nan_counts_and_json_nulls(tmp_path, inspect_zarr, capsys):
    ds = make_gridded()
    ds["precip"].values[0, 0, 0] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")

    run_skill(inspect_zarr, "-i", str(src), "--format", "json")

    precip = next(
        v for v in json.loads(capsys.readouterr().out)["data_vars"] if v["name"] == "precip"
    )
    assert precip["n_nan"] == 1
    assert precip["n_finite"] == 23
    assert precip["min"] == 1.0
    assert None not in precip["sample"]  # the sample shows finite values only


def test_data_sample_truncates(tmp_path, inspect_zarr, capsys):
    src = write_zarr(
        make_gridded(n_time=5, lats=tuple(range(8)), lons=tuple(range(8))), tmp_path / "in.zarr"
    )

    run_skill(inspect_zarr, "-i", str(src), "--max-values", "4")

    out = capsys.readouterr().out
    assert "finite 320/320" in out
    assert "…" in out
    assert "(4 of 320 finite cells)" in out


def test_max_values_zero_still_caps_data_sample(tmp_path, inspect_zarr, capsys):
    src = write_zarr(
        make_gridded(n_time=4, lats=tuple(range(10)), lons=tuple(range(10))),
        tmp_path / "in.zarr",
    )

    run_skill(inspect_zarr, "-i", str(src), "--format", "json", "--max-values", "0")

    precip = next(
        v for v in json.loads(capsys.readouterr().out)["data_vars"] if v["name"] == "precip"
    )
    assert precip["n"] == 400
    assert precip["sample_truncated"] is True
    # "…" plus at most the cap of 256 numeric cells.
    numeric = [v for v in precip["sample"] if v != "…"]
    assert len(numeric) == 256
    assert precip["sample"][-1] == "…"


def test_timedelta_coords_and_key_attrs_are_readable(tmp_path, inspect_zarr, capsys):
    ds = make_forecast(n_step=2)
    ds = ds.assign_coords(step=np.array([0, 3 * 3600], dtype="timedelta64[s]").astype("m8[ns]"))
    ds = xr.concat([ds, ds.assign_coords(step=ds.step + np.timedelta64(7, "D"))], dim="step")
    ds["tp"].attrs.update(cell_methods="step: mean", aggregation_period="7 day")
    src = write_zarr(ds, tmp_path / "in.zarr")

    run_skill(inspect_zarr, "-i", str(src))

    out = capsys.readouterr().out
    assert "0 days, 0 days 03:00:00, 7 days, 7 days 03:00:00" in out
    assert "long_name: Total precipitation" in out
    assert "aggregation_period: 7 day" in out
    assert "cell_methods: step: mean" in out


def test_sample_skips_leading_nan_and_datetime_stats(tmp_path, inspect_zarr, capsys):
    ds = make_gridded()
    ds["precip"].values[:, 0, :] = np.nan  # a clipped grid's empty first rows
    onset = np.full((3, 4), np.datetime64("NaT"), dtype="datetime64[ns]")
    onset[2, 3] = np.datetime64("2026-03-05")
    ds["onset"] = (("latitude", "longitude"), onset)
    src = write_zarr(ds, tmp_path / "in.zarr")
    run_skill(inspect_zarr, "-i", str(src), "--format", "json")
    by_name = {v["name"]: v for v in json.loads(capsys.readouterr().out)["data_vars"]}
    assert by_name["precip"]["sample"][:3] == [1.0, 1.0, 1.0]
    assert by_name["precip"]["n_nan"] == 8
    assert by_name["onset"]["n_finite"] == 1
    assert by_name["onset"]["min"] == "2026-03-05"
    assert by_name["onset"]["mean"] is None

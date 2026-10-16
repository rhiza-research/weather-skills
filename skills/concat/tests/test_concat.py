"""Correctness tests for concat."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def concat():
    return load_skill("concat", "concat").concat


def test_concat_along_new_dim_with_coords(tmp_path, concat):
    d1 = write_zarr(make_gridded(fill=1.0), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(fill=2.0), tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        concat,
        "-i",
        str(d1),
        "-i",
        str(d2),
        "-o",
        str(out),
        "--dim",
        "number",
        "--coords",
        "0,1",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["number"] == 2
    assert list(ds["number"].values) == [0, 1]
    assert float(ds["precip"].isel(number=0).mean()) == pytest.approx(1.0)
    assert float(ds["precip"].isel(number=1).mean()) == pytest.approx(2.0)
    assert load_history(out)[-1]["skill"] == "concat"


def test_concat_along_time(tmp_path, concat):
    d1 = write_zarr(make_gridded(n_time=2, start="2026-01-01"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(n_time=2, start="2026-01-03"), tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"

    run_skill(concat, "-i", str(d1), "-i", str(d2), "-o", str(out), "--dim", "time")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 4
    times = ds["time"].values.astype("datetime64[D]")
    assert times[0] == np.datetime64("2026-01-01")
    assert times[-1] == np.datetime64("2026-01-04")


def test_concat_rejects_single_input(tmp_path, concat):
    src = write_zarr(make_gridded(), tmp_path / "only.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(concat, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--dim", "time")
    assert exc.value.code == 2


def test_concat_converts_compatible_units(tmp_path, concat):
    hourly = make_gridded(fill=1.0)
    hourly["precip"].attrs["units"] = "mm hr-1"
    d1 = write_zarr(make_gridded(fill=1.0), tmp_path / "d1.zarr")
    d2 = write_zarr(hourly, tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"

    run_skill(concat, "-i", str(d1), "-i", str(d2), "-o", str(out), "--dim", "number")

    ds = xr.open_zarr(out, consolidated=True)
    assert float(ds["precip"].isel(number=1).mean()) == pytest.approx(24.0)
    assert ds["precip"].attrs["units"] == "mm day-1"


def test_concat_rejects_incompatible_units(tmp_path, concat):
    kelvin = make_gridded()
    kelvin["precip"].attrs["units"] = "K"
    d1 = write_zarr(make_gridded(), tmp_path / "d1.zarr")
    d2 = write_zarr(kelvin, tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(concat, "-i", str(d1), "-i", str(d2), "-o", str(out), "--dim", "number")
    assert exc.value.code == 2
    assert not out.exists()


def _run(concat, tmp_path, *paths, extra=()):
    out = tmp_path / "out.zarr"
    args = [a for p in paths for a in ("-i", str(p))]
    run_skill(concat, *args, "-o", str(out), *extra)
    return out


def test_concat_refuses_overlapping_time(tmp_path, concat):
    d1 = write_zarr(make_gridded(n_time=3, start="2026-01-01"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(n_time=3, start="2026-01-03"), tmp_path / "d2.zarr")
    with pytest.raises(SystemExit) as exc:
        _run(concat, tmp_path, d1, d2, extra=("--dim", "time"))
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_concat_sorts_out_of_order_inputs(tmp_path, concat):
    d1 = write_zarr(make_gridded(n_time=2, start="2026-01-03"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(n_time=2, start="2026-01-01"), tmp_path / "d2.zarr")
    out = _run(concat, tmp_path, d1, d2, extra=("--dim", "time"))
    times = xr.open_zarr(out)["time"].values
    assert (np.diff(times) > np.timedelta64(0)).all()


def test_concat_refuses_different_grid(tmp_path, concat):
    d1 = write_zarr(make_gridded(start="2026-01-01"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(start="2026-01-03", lats=(1.0, 2.0, 4.0)), tmp_path / "d2.zarr")
    with pytest.raises(SystemExit) as exc:
        _run(concat, tmp_path, d1, d2, extra=("--dim", "time"))
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_concat_refuses_different_windows_on_new_dim(tmp_path, concat):
    d1 = write_zarr(make_gridded(start="2026-01-01"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(start="2026-02-01"), tmp_path / "d2.zarr")
    with pytest.raises(SystemExit) as exc:
        _run(concat, tmp_path, d1, d2, extra=("--dim", "number"))
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_concat_refuses_coords_on_existing_dim(tmp_path, concat):
    d1 = write_zarr(make_gridded(start="2026-01-01"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(start="2026-01-03"), tmp_path / "d2.zarr")
    with pytest.raises(SystemExit) as exc:
        _run(concat, tmp_path, d1, d2, extra=("--dim", "time", "--coords", "0,1"))
    assert exc.value.code == 2


def test_concat_refuses_mixed_data_interval(tmp_path, concat):
    daily = make_gridded()
    daily["precip"].attrs["data_interval"] = "1 day"
    weekly = make_gridded()
    weekly["precip"].attrs["data_interval"] = "7 day"
    d1 = write_zarr(daily, tmp_path / "d1.zarr")
    d2 = write_zarr(weekly, tmp_path / "d2.zarr")
    with pytest.raises(SystemExit) as exc:
        _run(concat, tmp_path, d1, d2, extra=("--dim", "model"))
    assert exc.value.code == 2


def test_concat_coords_parse_dates(tmp_path, concat):
    day = make_gridded(n_time=1).isel(time=0, drop=True)
    d1 = write_zarr(day, tmp_path / "d1.zarr")
    d2 = write_zarr(day, tmp_path / "d2.zarr")
    out = _run(
        concat, tmp_path, d1, d2, extra=("--dim", "time", "--coords", "2026-09-01,2026-09-02")
    )
    times = xr.open_zarr(out)["time"].values
    assert times.dtype.kind == "M"
    assert times[1] == np.datetime64("2026-09-02")


def test_concat_refuses_variable_missing_from_an_input(tmp_path, concat, capsys):
    extra = make_gridded()
    extra["tmax"] = extra["precip"] * 0 + 30.0
    extra["tmax"].attrs["units"] = "degree_Celsius"
    d1 = write_zarr(make_gridded(), tmp_path / "d1.zarr")
    d2 = write_zarr(extra, tmp_path / "d2.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            concat, "-i", str(d1), "-i", str(d2), "-o", str(tmp_path / "o.zarr"), "--dim", "model"
        )
    assert exc.value.code == 2
    assert "different variables" in capsys.readouterr().err


def test_concat_stitches_adjacent_boxes_and_refuses_a_gap(tmp_path, concat, capsys):
    west = write_zarr(make_gridded(lons=(10.0, 11.0)), tmp_path / "w.zarr")
    east = write_zarr(make_gridded(lons=(12.0, 13.0)), tmp_path / "e.zarr")
    far = write_zarr(make_gridded(lons=(15.0, 16.0)), tmp_path / "f.zarr")
    out = tmp_path / "ok.zarr"
    run_skill(concat, "-i", str(west), "-i", str(east), "-o", str(out), "--dim", "longitude")
    assert list(xr.open_zarr(out).longitude.values) == [10.0, 11.0, 12.0, 13.0]
    with pytest.raises(SystemExit) as exc:
        run_skill(
            concat,
            "-i",
            str(west),
            "-i",
            str(far),
            "-o",
            str(tmp_path / "gap.zarr"),
            "--dim",
            "longitude",
        )
    assert exc.value.code == 2
    assert "not evenly spaced" in capsys.readouterr().err


def test_concat_refuses_mixed_calendars(tmp_path, concat, capsys):
    import cftime

    noleap = make_gridded(n_time=2, start="2026-01-05")
    noleap = noleap.assign_coords(time=[cftime.DatetimeNoLeap(2026, 1, 5 + i) for i in range(2)])
    a = write_zarr(make_gridded(), tmp_path / "a.zarr")
    b = write_zarr(noleap, tmp_path / "b.zarr")
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(concat, "-i", str(a), "-i", str(b), "-o", str(out), "--dim", "time")
    assert exc.value.code == 2
    assert "convert-calendar" in capsys.readouterr().err
    assert not out.exists()


def test_concat_single_time_inputs_keep_dates(tmp_path, concat):
    """select keeps a scalar time; stacking along time turns it back into an axis."""
    a = make_gridded(n_time=1, start="2026-09-14").isel(time=0)
    b = make_gridded(n_time=1, start="2026-09-21").isel(time=0)
    b = b.assign_coords(step=np.timedelta64(7, "D"))  # carried by one input only
    pa = write_zarr(a, tmp_path / "a.zarr")
    pb = write_zarr(b, tmp_path / "b.zarr")
    out = tmp_path / "out.zarr"

    run_skill(concat, "-i", str(pa), "-i", str(pb), "--dim", "time", "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-09-14", "2026-09-21"]
    assert "step" not in ds.coords

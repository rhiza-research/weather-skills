"""Correctness tests for convert-calendar."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def convert_calendar():
    return load_skill("convert-calendar", "convert_calendar").convert_calendar


def test_convert_calendar_to_noleap(tmp_path, convert_calendar):
    src = write_zarr(make_gridded(n_time=2), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_calendar, "-i", str(src), "-o", str(out), "--calendar", "noleap")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" in ds.dims
    assert ds.sizes["time"] == 2
    assert load_history(out)[-1]["skill"] == "convert-calendar"


def test_convert_calendar_360_day_needs_align_on(tmp_path, convert_calendar):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_calendar, "-i", str(src), "-o", str(out), "--calendar", "360_day")
    assert exc.value.code == 2


def test_convert_calendar_reports_dropped_dates(tmp_path, convert_calendar, capsys):
    # 2024-02-28 .. 2024-03-01: Feb 29 does not exist in noleap.
    src = write_zarr(make_gridded(n_time=3, start="2024-02-28"), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_calendar, "-i", str(src), "-o", str(out), "--calendar", "noleap")

    ds = xr.open_zarr(out, consolidated=True)
    assert [t.strftime("%m-%d") for t in ds["time"].values] == ["02-28", "03-01"]
    err = capsys.readouterr().err
    assert "dropped 1 of 3" in err
    assert "2024-02-29" in err


def test_convert_calendar_all_dates_dropped_is_usage_error(tmp_path, convert_calendar):
    src = write_zarr(make_gridded(n_time=1, start="2024-02-29"), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(convert_calendar, "-i", str(src), "-o", str(out), "--calendar", "noleap")
    assert exc.value.code == 2
    assert not out.exists()


def test_convert_calendar_timedelta_axis_is_usage_error(tmp_path, convert_calendar):
    src = write_zarr(make_forecast(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            convert_calendar,
            *("-i", str(src), "-o", str(out), "--calendar", "noleap", "--time-dim", "step"),
        )
    assert exc.value.code == 2


def test_convert_calendar_converts_time_bounds(tmp_path, convert_calendar):
    ds = make_gridded(n_time=2, start="2024-02-29")
    starts = ds["time"].values - np.timedelta64(1, "D")
    ds = ds.assign_coords(time_bounds=(("time", "nv"), np.stack([starts, ds["time"].values], 1)))
    ds["time"].attrs["bounds"] = "time_bounds"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(convert_calendar, "-i", str(src), "-o", str(out), "--calendar", "noleap")

    raw = xr.open_zarr(out, consolidated=True, decode_times=False)
    assert raw["time_bounds"].attrs["calendar"] == "noleap"
    got = xr.open_zarr(out, consolidated=True)
    # Only Mar 1 survives; its one-day cell starts on Feb 28 (no Feb 29 in noleap).
    edges = [t.strftime("%m-%d") for t in got["time_bounds"].values[0]]
    assert edges == ["02-28", "03-01"]

"""Correctness tests for merge."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def merge():
    return load_skill("merge", "merge").merge


def test_merge_keeps_shared_times(tmp_path, merge, capsys):
    rain = write_zarr(make_gridded(n_time=3, fill=2.0), tmp_path / "rain.zarr")
    temp = make_gridded(n_time=2, name="t2m", fill=25.0, start="2026-01-02")
    temp["t2m"].attrs = {"units": "degree_Celsius"}
    temp = write_zarr(temp, tmp_path / "temp.zarr")
    out = tmp_path / "out.zarr"

    run_skill(merge, "-i", str(rain), "-i", str(temp), "-o", str(out))

    ds = xr.open_zarr(out)
    assert set(ds.data_vars) == {"precip", "t2m"}
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-01-02", "2026-01-03"]
    assert float(ds["precip"].mean()) == pytest.approx(2.0)
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
    assert "kept the 2 of 3 'time' values" in capsys.readouterr().err


def test_merge_on_year(tmp_path, merge):
    """Inputs already on years (summarize-dim --by year) merge on their shared years."""
    iod = xr.Dataset(
        {"dmi": (("year",), [1.9, -0.2, -0.3, -1.0])}, coords={"year": [2019, 2020, 2021, 2022]}
    )
    rain = xr.Dataset(
        {"precip": (("year",), [380.0, 520.0, 610.0])}, coords={"year": [2018, 2019, 2020]}
    )
    rain["precip"].attrs["units"] = "mm"
    a = write_zarr(iod, tmp_path / "iod.zarr")
    b = write_zarr(rain, tmp_path / "rain.zarr")
    out = tmp_path / "out.zarr"

    run_skill(merge, "-i", str(a), "-i", str(b), "-o", str(out))

    ds = xr.open_zarr(out)
    assert dict(ds.sizes) == {"year": 2}
    assert list(ds["year"].values) == [2019, 2020]
    assert list(ds["dmi"].values) == pytest.approx([1.9, -0.2])
    assert list(ds["precip"].values) == pytest.approx([520, 610])


def test_errors(tmp_path, merge, capsys):
    rain = write_zarr(make_gridded(), tmp_path / "rain.zarr")
    later = write_zarr(make_gridded(name="other", start="2027-01-01"), tmp_path / "later.zarr")
    dup = make_gridded(n_time=3, name="dup")
    dup = write_zarr(dup.assign_coords(time=dup.time.values[[0, 0, 1]]), tmp_path / "dup.zarr")
    fc = write_zarr(
        make_forecast(lats=(1.0, 2.0, 3.0), lons=(10.0, 11.0, 12.0, 13.0)), tmp_path / "fc.zarr"
    )
    fc2 = write_zarr(make_forecast(name="t2m", init="2026-01-08"), tmp_path / "fc2.zarr")
    offset = write_zarr(
        make_gridded(name="other", lats=(1.5, 2.5), lons=(10.5, 11.5)), tmp_path / "offset.zarr"
    )
    cases = [
        (["-i", str(rain)], "at least two inputs"),
        (["-i", str(rain), "-i", str(dup)], "repeats values along 'time'"),
        (["-i", str(fc), "-i", str(rain)], "step-to-time"),
        (["-i", str(fc), "-i", str(fc2)], "different values of 'time'"),
        (["-i", str(rain), "-i", str(offset)], "coarsen --reference-grid"),
        (["-i", str(rain), "-i", str(rain)], "edit-coords --rename"),
        (["-i", str(rain), "-i", str(later)], "share no 'time' values"),
    ]
    for argv, match in cases:
        with pytest.raises(SystemExit):
            run_skill(merge, *argv, "-o", str(tmp_path / "x.zarr"))
        assert match in capsys.readouterr().err


def test_differing_bounds_fail(tmp_path, merge, capsys):
    """MSLP (instant) labels its steps [prev, step]; precip (avg) [step, next]: refuse."""
    hour = np.timedelta64(1, "h")
    steps = np.arange(3) * hour

    def _with_bounds(name, lo):
        ds = make_forecast(n_step=3, name=name).assign_coords(step=steps)
        ds = ds.assign_coords(step_bounds=(("step", "nv"), np.stack([lo, lo + hour], axis=1)))
        ds["step"].attrs["bounds"] = "step_bounds"
        return ds

    tp = write_zarr(_with_bounds("precip", steps), tmp_path / "tp.zarr")
    msl = write_zarr(_with_bounds("msl", steps - hour), tmp_path / "msl.zarr")
    with pytest.raises(SystemExit):
        run_skill(merge, "-i", str(msl), "-i", str(tp), "-o", str(tmp_path / "out.zarr"))
    assert "different intervals" in capsys.readouterr().err

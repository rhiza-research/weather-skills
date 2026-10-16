"""Correctness tests for select."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def select():
    return load_skill("select", "select_dim").select


def test_select_by_index_collapses_dim(tmp_path, select):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time", "--index", "1")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert load_history(out)[-1]["skill"] == "select"


def test_select_requires_index_or_value(tmp_path, select):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time")
    assert exc.value.code == 2


def test_select_value_station_id_stringdtype(tmp_path, select):
    """Zarr v3 stores pandas/object station ids as NumPy StringDType (kind T)."""
    import pandas as pd

    times = pd.to_datetime(["2026-08-19", "2026-08-20"])
    frames = []
    for sid in ("TA00072", "TA00131"):
        frames.append(
            pd.DataFrame(
                {"precip": [1.0, 2.0], "station_id": sid},
                index=times,
            )
        )
    long = pd.concat(frames)
    long.index.name = "time"
    long = long.reset_index().set_index(["time", "station_id"])
    ds = xr.Dataset.from_dataframe(long)
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    assert xr.open_zarr(src, consolidated=True)["station_id"].dtype.kind == "T"

    out = tmp_path / "out.zarr"
    run_skill(
        select,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "station_id",
        "--value",
        "TA00072",
        "--value",
        "TA00131",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert list(result["station_id"].values) == ["TA00072", "TA00131"]
    assert load_history(out)[-1]["skill"] == "select"


def test_select_one_station_keeps_identity_coords(tmp_path, select):
    import pandas as pd

    times = pd.to_datetime(["2026-08-19", "2026-08-20"])
    long = pd.DataFrame(
        {
            "precip": [1.0, 2.0, 3.0, 4.0],
            "station_id": ["TA00072", "TA00072", "TA00131", "TA00131"],
            "time": list(times) * 2,
        }
    ).set_index(["time", "station_id"])
    ds = xr.Dataset.from_dataframe(long)
    ds = ds.assign_coords(
        name=("station_id", ["Likoni", "Shimo la Tewa"]),
        latitude=("station_id", [-4.08, -3.99]),
        longitude=("station_id", [39.66, 39.74]),
    )
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "one.zarr"
    run_skill(
        select,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "station_id",
        "--value",
        "TA00072",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert "station_id" not in result.dims
    assert str(result["station_id"].values) == "TA00072"
    assert str(result["name"].values) == "Likoni"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--dim", "nosuch", "--index", "0"], "not a dimension"),
        (["--dim", "time", "--index", "3"], "out of range for 'time' (size 3)"),
        (["--dim", "time", "--index", "-4"], "out of range"),
        (["--dim", "time", "--value", "garbage"], "not an ISO datetime"),
        (["--dim", "time", "--value", "2027-01-01"], "values are 2026-01-01, 2026-01-02"),
    ],
)
def test_select_bad_selectors_exit_2(tmp_path, select, capsys, args, message):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(select, "-i", str(src), "-o", str(tmp_path / "out.zarr"), *args)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_select_value_rejects_dim_without_coord(tmp_path, select, capsys):
    ds = make_gridded().expand_dims(member=3)
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            select,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--dim",
            "member",
            "--value",
            "1",
        )
    assert exc.value.code == 2
    assert "select it with --index" in capsys.readouterr().err


def test_select_value_float_label_from_arithmetic(tmp_path, select):
    ds = make_gridded(lats=(0.1, 0.1 + 0.2, 0.5))  # 0.30000000000000004
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "latitude", "--value", "0.3")
    assert "latitude" not in xr.open_zarr(out, consolidated=True).dims


def test_select_value_cftime_360_day(tmp_path, select):
    import cftime
    import numpy as np

    times = [cftime.Datetime360Day(2026, 2, d) for d in (29, 30)]
    ds = xr.Dataset({"tas": (("time",), np.array([1.0, 2.0]))}, coords={"time": times})
    ds["tas"].attrs["units"] = "K"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time", "--value", "2026-02-30")
    assert float(xr.open_zarr(out, consolidated=True)["tas"]) == 2.0


def test_select_refuses_repeated_dim(tmp_path, select, capsys):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    argv = ["-i", str(src), "-o", str(out), "--dim", "time", "--dim", "latitude", "--index", "0"]
    with pytest.raises(SystemExit) as exc:
        run_skill(select, *argv)
    assert exc.value.code == 2
    assert "--dim may be given once" in capsys.readouterr().err
    assert not out.exists()


def test_select_one_entry_keeps_label_as_scalar_coord(tmp_path, select):
    """verify and plot need the picked date after the time dim is gone."""
    src = write_zarr(make_gridded(n_time=3, start="2026-09-14"), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time", "--value", "2026-09-15")

    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert ds["time"].ndim == 0
    assert str(ds["time"].values)[:10] == "2026-09-15"

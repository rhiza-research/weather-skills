"""Correctness tests for csv-io."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def csv_io():
    return load_skill("csv-io", "csv_io").csv_io


def _open(path):
    return xr.open_zarr(path, consolidated=True)


def _write(tmp_path, text, name="in.csv"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_export_grid_puts_units_in_header(tmp_path, csv_io):
    ds = make_gridded(n_time=2, lats=(1.0, 2.0), lons=(10.0, 11.0, 12.0))
    ds["precip"].values[:] = np.arange(12).reshape(2, 2, 3)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.csv"

    run_skill(csv_io, "-i", str(src), "-o", str(out))

    df = pd.read_csv(out)
    assert list(df.columns) == ["time", "latitude", "longitude", "precip [mm d-1]"]
    assert len(df) == 12
    assert df["time"].iloc[0] == "2026-01-01"
    np.testing.assert_allclose(df["precip [mm d-1]"], np.arange(12))


def test_grid_round_trip(tmp_path, csv_io):
    ds = make_gridded(n_time=3)
    ds["precip"].values[:] = np.random.default_rng(0).random(ds["precip"].shape)
    ds["precip"].values[1, 0, 0] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))

    res = _open(back)
    assert res["precip"].dims == ("time", "latitude", "longitude")
    np.testing.assert_allclose(res["precip"].values, ds["precip"].values, equal_nan=True)
    assert units_equal(res["precip"].attrs["units"], "mm day-1")
    assert load_history(back)[-1]["skill"] == "csv-io"
    assert res.attrs["weather_skills_source"] == "csv:out.csv"


def test_export_dropna_skips_empty_rows(tmp_path, csv_io):
    ds = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0))
    ds["precip"].values[0, 0, :] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.csv"

    run_skill(csv_io, "-i", str(src), "-o", str(out), "--dropna")

    assert len(pd.read_csv(out)) == 2


def test_point_obs_round_trip_keeps_station_coords(tmp_path, csv_io):
    ds = make_point_obs(n_time=3, n_points=2)
    ds["precip"].values[:] = [[1.0, 2.0, np.nan], [4.0, 5.0, 6.0]]
    ds = ds.assign_coords(name=("point_id", ["Nairobi", "Mombasa"]))
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))

    res = _open(back)
    assert res["precip"].dims == ("station_id", "time")
    assert list(res["name"].values) == ["Nairobi", "Mombasa"]
    np.testing.assert_allclose(res["latitude"].values, [1.0, 2.0])
    np.testing.assert_allclose(res["precip"].values, ds["precip"].values, equal_nan=True)
    assert res["station_id"].attrs["cf_role"] == "timeseries_id"
    assert res.attrs["featureType"] == "timeSeries"


def test_forecast_round_trip_keeps_step_and_init(tmp_path, csv_io):
    ds = make_forecast(n_step=3)
    ds["tp"].values[:] = np.arange(12).reshape(3, 2, 2)
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    assert "step [days]" in pd.read_csv(csv).columns
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))

    res = _open(back)
    assert res["tp"].dims == ("step", "latitude", "longitude")
    assert res["step"].values[1] == np.timedelta64(1, "D")
    assert res["time"].ndim == 0
    assert res["time"].attrs["standard_name"] == "forecast_reference_time"
    np.testing.assert_allclose(res["tp"].values, ds["tp"].values)


def test_read_station_sheet(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "Station,Station Name,Lat,Lon,Date,Rainfall (mm),Tmax (degC),Remarks\n"
        "9136164,Dagoretti,-1.30,36.75,01/10/2026,0.0,24.1,\n"
        "9136164,Dagoretti,-1.30,36.75,02/10/2026,12.4,22.8,storm\n"
        "9136164,Dagoretti,-1.30,36.75,03/10/2026,-99,23.5,gauge fault\n"
        "9039000,Mombasa,-4.03,39.62,01/10/2026,3.2,30.2,\n"
        "9039000,Mombasa,-4.03,39.62,02/10/2026,0.0,31.0,\n",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        csv_io, "--csv", str(src), "-o", str(out),
        "--date-format", "%d/%m/%Y", "--na-value", "-99",
    )  # fmt: skip

    res = _open(out)
    assert res["Rainfall"].dims == ("station_id", "time")
    assert "Remarks" not in res
    assert list(res["Station_Name"].values) == ["Mombasa", "Dagoretti"]
    assert units_equal(res["Rainfall"].attrs["units"], "mm")
    assert units_equal(res["Tmax"].attrs["units"], "degC")
    assert str(res["time"].values[0])[:10] == "2026-10-01"
    rain = res["Rainfall"].sel(station_id="9136164").values
    np.testing.assert_allclose(rain, [0.0, 12.4, np.nan])
    # A row missing from the sheet stays missing.
    assert np.isnan(res["Rainfall"].sel(station_id="9039000").values[2])


def test_read_keeps_leading_zeros_in_station_ids(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain\n00123,1,10,2026-10-01,1\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    assert list(_open(out)["station_id"].values) == ["00123"]


def test_read_refuses_ambiguous_dates(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain\nA,1,10,01/10/2026,1\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_read_single_site_with_point(tmp_path, csv_io):
    src = _write(tmp_path, "date,rain\n2026-10-01,1.5\n2026-10-02,0\n2026-10-03,7.25\n")
    out = tmp_path / "out.zarr"

    run_skill(
        csv_io, "--csv", str(src), "-o", str(out),
        "--point=-1.29,36.82,nairobi", "--units", "rain=mm",
    )  # fmt: skip

    res = _open(out)
    assert list(res["station_id"].values) == ["nairobi"]
    np.testing.assert_allclose(res["latitude"].values, [-1.29], rtol=1e-5)
    np.testing.assert_allclose(res["rain"].values, [[1.5, 0.0, 7.25]])
    assert units_equal(res["rain"].attrs["units"], "mm")


def test_read_rejects_duplicate_rows(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain\nA,1,10,2026-10-01,1\nA,1,10,2026-10-01,2\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_needs_exactly_one_direction(tmp_path, csv_io):
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_export_refuses_too_many_rows(tmp_path, csv_io):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "-i", str(src), "-o", str(tmp_path / "out.csv"), "--max-rows", "5")
    assert exc.value.code == 2

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


def test_read_excel_sheet(tmp_path, csv_io):
    src = tmp_path / "in.xlsx"
    df = pd.DataFrame(
        {
            "Station": ["00123", "00123", "00456"],
            "Lat": [-1.3, -1.3, -4.0],
            "Lon": [36.8, 36.8, 39.6],
            "Date": pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-01"]),
            "Rainfall (mm)": [0.0, -99, 3.2],
        }
    )
    with pd.ExcelWriter(src) as writer:
        pd.DataFrame({"notes": ["cover sheet"]}).to_excel(writer, sheet_name="About", index=False)
        df.to_excel(writer, sheet_name="Daily", index=False)
    out = tmp_path / "out.zarr"

    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--sheet", "Daily", "--na-value", "-99")

    res = _open(out)
    assert res["Rainfall"].dims == ("station_id", "time")
    assert sorted(res["station_id"].values) == ["00123", "00456"]
    assert units_equal(res["Rainfall"].attrs["units"], "mm")
    np.testing.assert_allclose(res["Rainfall"].sel(station_id="00123").values, [0.0, np.nan])
    with pytest.raises(SystemExit):
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "x.zarr"), "--sheet", "Nope")


def test_read_wide_sheet_with_unpivot(tmp_path, csv_io):
    # A met-service sheet: title line, blank headers, one column per day, a total, "tr".
    src = _write(
        tmp_path,
        "DAILY RAINFALL OCTOBER 2026\n"
        ",,,15th,16th,17th,Total\n"
        "35.6,3.1,LODWAR,0.0,tr,1.0,1.0\n"
        ",,,,,,\n"
        "39.1,3.5,MOYALE,4.1,,tr,4.1\n",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        csv_io, "--csv", str(src), "-o", str(out), "--skip-rows", "1",
        "--names", "lon,lat,station,2026-10-15,2026-10-16,2026-10-17,total",
        "--drop-column", "total", "--replace", "tr=0",
        "--unpivot", "time=precip", "--units", "precip=mm",
    )  # fmt: skip

    res = _open(out)
    assert res["precip"].dims == ("station_id", "time")
    assert sorted(res["station_id"].values) == ["LODWAR", "MOYALE"]
    assert str(res["time"].values[0])[:10] == "2026-10-15"
    assert units_equal(res["precip"].attrs["units"], "mm")
    assert float(res["longitude"].sel(station_id="MOYALE")) == pytest.approx(39.1)
    np.testing.assert_allclose(res["precip"].sel(station_id="LODWAR").values, [0.0, 0.0, 1.0])
    # A blank cell stays missing; only the "tr" code becomes 0.
    np.testing.assert_allclose(res["precip"].sel(station_id="MOYALE").values, [4.1, np.nan, 0.0])


def test_unpivot_refuses_unmapped_text(tmp_path, csv_io):
    src = _write(tmp_path, "lon,lat,station,2026-10-15\n35.6,3.1,LODWAR,tr\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"),
                  "--unpivot", "time=precip")  # fmt: skip
    assert exc.value.code == 2


def test_names_must_match_column_count(tmp_path, csv_io):
    src = _write(tmp_path, ",,,15th\n35.6,3.1,LODWAR,0\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"),
                  "--names", "lon,lat,station")  # fmt: skip
    assert exc.value.code == 2


def test_read_keeps_leading_zeros_in_station_ids(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain\n00123,1,10,2026-10-01,1\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    assert list(_open(out)["station_id"].values) == ["00123"]


def test_read_keeps_leading_zeros_under_a_spaced_header(tmp_path, csv_io):
    src = _write(tmp_path, "Station ID,lat,lon,date,rain\n00123,1,10,2026-10-01,1\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    assert list(_open(out)["station_id"].values) == ["00123"]


def test_station_round_trip_keeps_site_and_pressure(tmp_path, csv_io):
    # A 3dpaws-fetch-like store: station_id dim, a site coord, station pressure in hPa.
    ds = make_point_obs(n_time=2, n_points=2).rename(point_id="station_id")
    ds = ds.assign_coords(station_id=["1", "4"], site=("station_id", ["KMD HQ", "Kitui"]))
    ds["pressure"] = (("station_id", "time"), [[850.5, 851.0], [892.5, np.nan]])
    ds["pressure"].attrs["units"] = "hPa"
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    assert {"station_id", "site", "pressure [hPa]"} <= set(pd.read_csv(csv).columns)
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))

    res = _open(back)
    assert list(res["station_id"].values) == ["1", "4"]
    assert list(res["site"].values) == ["KMD HQ", "Kitui"]
    assert res["pressure"].dims == ("station_id", "time")
    assert "level" not in res.dims
    np.testing.assert_allclose(res["pressure"].values, ds["pressure"].values, equal_nan=True)


def test_read_station_pressure_is_data_not_a_level(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station_id,latitude,longitude,time,pressure [hPa],precip [mm day-1]\n"
        "TA00025,-1.3,36.8,2026-10-01,850.5,0\nTA00025,-1.3,36.8,2026-10-02,851.0,2\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    res = _open(out)
    assert res["pressure"].dims == ("station_id", "time")
    assert res["pressure"].attrs["units"] == "hPa"


def test_non_standard_calendar_round_trip(tmp_path, csv_io):
    ds = make_gridded(n_time=3, lats=(1.0,), lons=(10.0,))
    times = xr.date_range("2026-02-29", periods=3, freq="D", calendar="360_day", use_cftime=True)
    ds = ds.assign_coords(time=times)
    ds["precip"].values[:, 0, 0] = [1.0, 2.0, 3.0]
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    df = pd.read_csv(csv)
    assert list(df["time [360_day]"]) == ["2026-02-29", "2026-02-30", "2026-03-01"]
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))

    res = _open(back)
    assert res["time"].dt.calendar == "360_day"
    assert [t.strftime("%Y-%m-%d") for t in res["time"].values] == list(df["time [360_day]"])
    np.testing.assert_allclose(res["precip"].values[:, 0, 0], [1.0, 2.0, 3.0])


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
        "--point", "-1.29,36.82,nairobi", "--units", "rain=mm",
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


def test_export_refuses_four_dims(tmp_path, csv_io, capsys):
    ds = make_gridded(n_time=2).expand_dims(number=[0, 1])
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "-i", str(src), "-o", str(tmp_path / "out.csv"))
    assert exc.value.code == 2
    assert "summarize-dim" in capsys.readouterr().err
    assert not (tmp_path / "out.csv").exists()


def test_na_value_and_date_format_round_trip(tmp_path, csv_io):
    ds = make_point_obs(n_time=2, n_points=1)
    ds["precip"].values[:] = [[np.nan, 3.0]]
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"
    opts = ["--na-value", "-999", "--date-format", "%d/%m/%Y"]

    run_skill(csv_io, "-i", str(src), "-o", str(csv), *opts)
    df = pd.read_csv(csv)
    assert list(df["time"]) == ["01/01/2026", "02/01/2026"]
    assert df["precip [mm d-1]"].iloc[0] == -999
    run_skill(csv_io, "--csv", str(csv), "-o", str(back), *opts)

    res = _open(back)
    np.testing.assert_allclose(res["precip"].values, ds["precip"].values, equal_nan=True)
    assert res["time"].values[1] == np.datetime64("2026-01-02")


def test_pivot_round_trips_with_unpivot(tmp_path, csv_io):
    ds = make_gridded(n_time=3, lats=(-1.0, 2.0), lons=(10.0, 11.0))
    ds["precip"].values[:] = np.arange(12).reshape(3, 2, 2)
    ds["precip"].values[1, 0, 0] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv), "--pivot", "time=precip")
    df = pd.read_csv(csv)
    assert list(df.columns) == ["latitude", "longitude", "2026-01-01", "2026-01-02", "2026-01-03"]
    assert len(df) == 4
    run_skill(
        csv_io,
        "--csv",
        str(csv),
        "-o",
        str(back),
        "--unpivot",
        "time=precip",
        "--units",
        "precip=mm day-1",
    )

    res = _open(back)
    assert res["precip"].dims == ("time", "latitude", "longitude")
    np.testing.assert_allclose(res["precip"].values, ds["precip"].values, equal_nan=True)


def test_pivot_steps_read_back_as_leads(tmp_path, csv_io):
    ds = make_forecast(n_step=3)
    ds["tp"].values[:] = np.arange(12).reshape(3, 2, 2)
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv), "--pivot", "step=tp")
    run_skill(csv_io, "--csv", str(csv), "-o", str(back), "--unpivot", "step=tp")

    res = _open(back)
    assert res["step"].values[1] == np.timedelta64(1, "D")
    np.testing.assert_allclose(res["tp"].values, ds["tp"].values)


def test_pivot_needs_a_dim_and_a_variable(tmp_path, csv_io):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    for spec in ("level=precip", "time=tmax", "time"):
        with pytest.raises(SystemExit) as exc:
            run_skill(csv_io, "-i", str(src), "-o", str(tmp_path / "out.csv"), "--pivot", spec)
        assert exc.value.code == 2


def test_excel_round_trip(tmp_path, csv_io):
    ds = make_point_obs(n_time=3, n_points=2)
    ds["precip"].values[:] = [[1.0, 2.0, np.nan], [4.0, 5.0, 6.0]]
    src = write_zarr(ds, tmp_path / "in.zarr")
    xlsx = tmp_path / "out.xlsx"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(xlsx), "--sheet", "rain")
    assert pd.ExcelFile(xlsx).sheet_names == ["rain"]
    run_skill(csv_io, "--csv", str(xlsx), "-o", str(back), "--sheet", "rain")

    res = _open(back)
    np.testing.assert_allclose(res["precip"].values, ds["precip"].values, equal_nan=True)


def test_options_go_with_their_direction(tmp_path, csv_io):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    csv = _write(tmp_path, "lat,lon,rain\n1,10,1\n")
    for args in (
        ["-i", str(src), "-o", str(tmp_path / "out.csv"), "--unpivot", "time=precip"],
        ["-i", str(src), "-o", str(tmp_path / "out.csv"), "--na-value", "-9", "--na-value", "-8"],
        ["-i", str(src), "-o", str(tmp_path / "out.csv"), "--sheet", "rain"],
        ["--csv", str(csv), "-o", str(tmp_path / "out.zarr"), "--pivot", "time=rain"],
    ):
        with pytest.raises(SystemExit) as exc:
            run_skill(csv_io, *args)
        assert exc.value.code == 2


def test_read_refuses_headers_that_clean_to_one_name(tmp_path, csv_io):
    src = _write(tmp_path, "lat,lon,rain mm,rain-mm\n1,10,1,2\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_replace_makes_a_text_column_numeric(tmp_path, csv_io):
    # "T" (trace) makes the whole column text; --replace must leave numbers behind.
    src = _write(
        tmp_path,
        "Station,Lat,Lon,Date,Rainfall (mm)\n"
        "A,1,10,2026-10-01,12.4\nA,1,10,2026-10-02,T\nA,1,10,2026-10-03,-99\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--replace", "T=0", "--na-value", "-99")
    np.testing.assert_allclose(_open(out)["Rainfall"].values, [[12.4, 0.0, np.nan]])


def test_replace_in_excel_with_numbers_stored_as_text(tmp_path, csv_io):
    src = tmp_path / "in.xlsx"
    pd.DataFrame(
        {"station": ["A"] * 3, "lat": [1] * 3, "lon": [10] * 3,
         "date": pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-03"]),
         "rain [mm]": [1.5, "tr", "2.5"]}
    ).to_excel(src, index=False)  # fmt: skip
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--replace", "tr=0")
    np.testing.assert_allclose(_open(out)["rain"].values, [[1.5, 0.0, 2.5]])


def test_na_words_are_missing_only_in_number_columns(tmp_path, csv_io):
    # "NA" is Namibia as a station id or country, but missing in a rain column.
    src = _write(
        tmp_path,
        "station_id,country,lat,lon,date,rain [mm]\n"
        "NA,NA,-22.5,17,2026-10-01,1\nNA,NA,-22.5,17,2026-10-02,NA\nZA1,ZA,-26,28,2026-10-01,2\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    res = _open(out)
    assert list(res["station_id"].values) == ["NA", "ZA1"]
    assert list(res["country"].values) == ["NA", "ZA"]
    np.testing.assert_allclose(res["rain"].sel(station_id="NA").values, [1.0, np.nan])


def test_read_converts_time_offsets_to_utc(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,time,t [degC]\n"
        "A,0,36,2026-10-01T00:00Z,15\nA,0,36,2026-10-01T04:00+03:00,16\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    times = _open(out)["time"].values
    assert list(times) == [np.datetime64("2026-10-01T00:00"), np.datetime64("2026-10-01T01:00")]


def test_read_names_the_bad_date(tmp_path, csv_io, capsys):
    src = _write(tmp_path, "station,lat,lon,date,rain\nA,1,10,2026-10-01,1\nA,1,10,02/10/2026,2\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2
    assert "'02/10/2026'" in capsys.readouterr().err


def test_station_pivot_round_trips_with_text_coords(tmp_path, csv_io):
    ds = make_point_obs(n_time=3, n_points=2).rename(point_id="station_id")
    ds = ds.assign_coords(name=("station_id", ["KMD HQ", "Kitui"]))
    ds["precip"].values[:] = [[1.0, np.nan, 3.0], [4.0, 5.0, 6.0]]
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv), "--pivot", "time=precip")
    run_skill(
        csv_io, "--csv", str(csv), "-o", str(back),
        "--unpivot", "time=precip", "--units", "precip=mm day-1",
    )  # fmt: skip

    res = _open(back)
    assert list(res["name"].values) == ["KMD HQ", "Kitui"]
    np.testing.assert_allclose(res["precip"].values, ds["precip"].values, equal_nan=True)


def test_unpivot_keeps_excel_date_headers_as_dates(tmp_path, csv_io):
    src = tmp_path / "in.xlsx"
    df = pd.DataFrame([[36.8, -1.3, "Dagoretti", 1.0, 2.0]])
    df.columns = ["lon", "lat", "station", pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")]
    df.to_excel(src, index=False)
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--unpivot", "time=precip")
    res = _open(out)
    assert list(res["time"].values) == [np.datetime64("2026-10-15"), np.datetime64("2026-10-16")]
    np.testing.assert_allclose(res["precip"].values, [[1.0, 2.0]])


def test_unpivot_keeps_index_columns(tmp_path, csv_io):
    # A county table: names in a column no layout rule knows, one column per dekad.
    src = _write(tmp_path, "Feature,ire198101,ire198102\nAinabkoi,9.7,0.6\nAldai,17.8,5.0\n")
    out = tmp_path / "out.zarr"
    run_skill(
        csv_io, "--csv", str(src), "-o", str(out),
        "--unpivot", "dekad=precip", "--index", "Feature", "--index", "dekad",
    )  # fmt: skip
    res = _open(out)
    assert res["precip"].dims == ("Feature", "dekad")
    np.testing.assert_allclose(res["precip"].sel(Feature="Aldai").values, [17.8, 5.0])


def test_export_refuses_date_format_that_drops_hours(tmp_path, csv_io):
    ds = make_gridded(n_time=2, lats=(1.0,), lons=(10.0,))
    ds = ds.assign_coords(time=pd.date_range("2026-10-01 06:00", periods=2, freq="6h"))
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "-i", str(src), "-o", str(tmp_path / "out.csv"),
                  "--date-format", "%d/%m/%Y")  # fmt: skip
    assert exc.value.code == 2
    assert not (tmp_path / "out.csv").exists()


def test_scalar_level_reads_back_as_scalar_and_re_exports(tmp_path, csv_io):
    ds = make_gridded(n_time=2, lats=(1.0, 2.0), lons=(10.0, 11.0)).assign_coords(level=850)
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))

    res = _open(back)
    assert res["precip"].dims == ("time", "latitude", "longitude")
    assert int(res["level"]) == 850


def test_export_counts_only_dims_longer_than_one(tmp_path, csv_io):
    # (time: 1, level, latitude, longitude): a one-day pressure-level grid.
    ds = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0)).expand_dims(level=[500, 850])
    src = write_zarr(ds.transpose("time", "level", ...), tmp_path / "in.zarr")
    out = tmp_path / "out.csv"
    run_skill(csv_io, "-i", str(src), "-o", str(out))
    assert len(pd.read_csv(out)) == 8


def test_export_leaves_out_bounds_dims(tmp_path, csv_io):
    ds = make_gridded(n_time=2, lats=(1.0,), lons=(10.0,))
    bounds = np.stack([ds["time"].values, ds["time"].values + np.timedelta64(1, "D")], axis=1)
    ds = ds.assign_coords(time_bounds=(("time", "nv"), bounds))
    ds["time"].attrs["bounds"] = "time_bounds"
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.csv"
    run_skill(csv_io, "-i", str(src), "-o", str(out))
    df = pd.read_csv(out)
    assert len(df) == 2
    assert "nv" not in df.columns and "time_bounds" not in df.columns


def test_time_series_round_trip(tmp_path, csv_io):
    ds = make_gridded(n_time=3, lats=(1.0,), lons=(10.0,)).isel(latitude=0, longitude=0)
    ds = ds.drop_vars(["latitude", "longitude"])
    ds["precip"].values[:] = [0.2, -0.1, 0.3]
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"

    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))

    res = _open(back)
    assert res["precip"].dims == ("time",)
    np.testing.assert_allclose(res["precip"].values, [0.2, -0.1, 0.3])


def test_export_single_value(tmp_path, csv_io):
    ds = make_gridded(n_time=1, lats=(1.0,), lons=(10.0,)).isel(time=0, latitude=0, longitude=0)
    src = write_zarr(ds.drop_vars(["latitude", "longitude"]), tmp_path / "in.zarr")
    out = tmp_path / "out.csv"
    run_skill(csv_io, "-i", str(src), "-o", str(out))
    df = pd.read_csv(out)
    assert list(df.columns) == ["time", "precip [mm d-1]"]
    assert df["precip [mm d-1]"].iloc[0] == 1.0


def test_export_dropna_with_nothing_left_exits_2(tmp_path, csv_io):
    ds = make_gridded(n_time=1, lats=(1.0,), lons=(10.0,), fill=np.nan)
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "-i", str(src), "-o", str(tmp_path / "out.csv"), "--dropna")
    assert exc.value.code == 2


def test_read_windows_1252_csv(tmp_path, csv_io):
    src = tmp_path / "in.csv"
    src.write_bytes("station,name,lat,lon,date,rain [mm]\nS1,Garoua Boulaï,7,13,2026-10-01,3.2\n"
                    .encode("cp1252"))  # fmt: skip
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    assert list(_open(out)["name"].values) == ["Garoua Boulaï"]


def test_read_semicolons_with_decimal_commas(tmp_path, csv_io):
    src = _write(tmp_path, "station;lat;lon;date;rain [mm]\nS1;-1,3;36,8;2026-10-01;2,5\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    res = _open(out)
    np.testing.assert_allclose(res["rain"].values, [[2.5]])
    np.testing.assert_allclose(res["latitude"].values, [-1.3], rtol=1e-6)


def test_read_empty_file_exits_2(tmp_path, csv_io):
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(_write(tmp_path, "")), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_read_names_text_columns_when_no_numbers_remain(tmp_path, csv_io, capsys):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm]\nA,1,10,2026-10-01,T\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2
    assert "rain [mm] has text cells such as 'T'" in capsys.readouterr().err


def test_read_refuses_scattered_points(tmp_path, csv_io):
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"lat": rng.uniform(-5, 5, 400), "lon": rng.uniform(33, 42, 400), "rain": 1})
    src = tmp_path / "in.csv"
    df.to_csv(src, index=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_read_drops_empty_column_under_a_trailing_comma(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm],\nA,1,10,2026-10-01,1,\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    assert list(_open(out).data_vars) == ["rain"]


def test_daily_dates_with_an_offset_keep_their_local_date(tmp_path, csv_io):
    # Local midnight +03:00 is 21:00 UTC the day before; a daily total keeps its own date.
    src = _write(
        tmp_path,
        "station,lat,lon,date,rain [mm]\n"
        "A,0,36,2026-10-01T00:00+03:00,1\nA,0,36,2026-10-02T00:00+03:00,2\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    assert list(_open(out)["time"].values) == [
        np.datetime64("2026-10-01"),
        np.datetime64("2026-10-02"),
    ]


def test_read_refuses_times_mixing_offsets_and_none(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,time,t [degC]\nA,0,36,2026-10-01T03:00+03:00,1\nA,0,36,2026-10-01 01:00,2\n",
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


@pytest.mark.parametrize("zone", ["Africa/Nairobi", "+03:00", "UTC+3"])
def test_timezone_turns_local_times_into_utc(tmp_path, csv_io, zone):
    src = _write(
        tmp_path,
        "station,lat,lon,time,t [degC]\nA,0,36,2026-10-01 03:00,1\nA,0,36,2026-10-01 04:00,2\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--timezone", zone)
    assert list(_open(out)["time"].values) == [
        np.datetime64("2026-10-01T00:00"),
        np.datetime64("2026-10-01T01:00"),
    ]


def test_timezone_leaves_dates_alone(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm]\nA,0,36,2026-10-01,1\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--timezone", "Africa/Nairobi")
    assert _open(out)["time"].values[0] == np.datetime64("2026-10-01")


def test_timezone_round_trip_names_the_zone_in_the_header(tmp_path, csv_io):
    ds = make_point_obs(n_time=2, n_points=1)
    ds = ds.assign_coords(time=pd.date_range("2026-10-01 00:00", periods=2, freq="h"))
    src = write_zarr(ds, tmp_path / "in.zarr")
    for zone, first in (("Africa/Nairobi", "2026-10-01 03:00:00"), (None, "2026-10-01 00:00:00")):
        csv = tmp_path / f"out{zone is None}.csv"
        back = tmp_path / f"back{zone is None}.zarr"
        run_skill(csv_io, "-i", str(src), "-o", str(csv), *(["--timezone", zone] if zone else []))
        df = pd.read_csv(csv)
        assert df[f"time [{zone or 'UTC'}]"].iloc[0] == first
        run_skill(csv_io, "--csv", str(csv), "-o", str(back))
        np.testing.assert_array_equal(_open(back)["time"].values, ds["time"].values)


def test_timezone_refuses_unknown_zones_and_dst_gaps(tmp_path, csv_io):
    local = _write(tmp_path, "station,lat,lon,time,t\nA,40,-74,2026-03-08 02:30,1\n")
    for zone in ("EAT", "America/New_York"):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                csv_io, "--csv", str(local), "-o", str(tmp_path / "o.zarr"), "--timezone", zone
            )
        assert exc.value.code == 2


def _exits_2(csv_io, *args):
    with pytest.raises(SystemExit) as exc:
        run_skill(csv_io, *args)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "rows",
    [
        "A,1,36,2026-10-01,1\nA,1,36,,2\n",  # no date
        "A,1,36,2026-10-01,1\n,1,37,2026-10-01,2\n",  # no station
    ],
)
def test_rows_without_a_key_exit_2(tmp_path, csv_io, rows):
    # pandas would put such a row on another row's label, overwriting its value.
    src = _write(tmp_path, "station,lat,lon,date,rain [mm]\n" + rows)
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))


def test_drop_incomplete_leaves_out_footer_rows(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,date,rain [mm]\nA,1,36,2026-10-01,1\nA,1,36,2026-10-02,2\nTotal,,,,3\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--drop-incomplete")
    res = _open(out)
    assert list(res["station_id"].values) == ["A"]
    np.testing.assert_allclose(res["rain"].values, [[1.0, 2.0]])


def test_codes_and_ids_ignore_stray_spaces(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,date,rain [mm]\n"
        "A ,1,36,2026-10-01,T \nA,1,36,2026-10-02,-99 \nA,1,36,2026-10-03, 4\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--replace", "T=0", "--na-value", "-99")
    res = _open(out)
    assert list(res["station_id"].values) == ["A"]
    np.testing.assert_allclose(res["rain"].values, [[0.0, np.nan, 4.0]])


def test_codes_leave_station_ids_alone(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,date,rain [mm]\nT,1,36,2026-10-01,T\n-99,2,37,2026-10-01,-99\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--replace", "T=0", "--na-value", "-99")
    res = _open(out)
    assert sorted(res["station_id"].values) == ["-99", "T"]
    np.testing.assert_allclose(res["rain"].sel(station_id="T").values, [0.0])


@pytest.mark.parametrize("lat_lon", ["95,36", "1,400", "1S,36", "36,-200"])
def test_read_refuses_bad_coordinates(tmp_path, csv_io, lat_lon):
    src = _write(tmp_path, f"station,lat,lon,date,rain [mm]\nA,{lat_lon},2026-10-01,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))


def test_station_longitudes_read_in_minus_180_to_180(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm]\nA,1,200,2026-10-01,1\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    np.testing.assert_allclose(_open(out)["longitude"].values, [-160.0])


def test_read_refuses_stations_without_a_location(tmp_path, csv_io):
    src = _write(
        tmp_path, "station,lat,lon,date,rain [mm]\nA,1,36,2026-10-01,1\nB,,,2026-10-01,2\n"
    )
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"))


@pytest.mark.parametrize("point", ["95,36", "1,400,x", "1,36,", "nan,36"])
def test_point_refuses_bad_values(tmp_path, csv_io, point):
    src = _write(tmp_path, "date,rain [mm]\n2026-10-01,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"), "--point", point)


@pytest.mark.parametrize(
    "header,extra",
    [
        ("station,lat,lon,date,rain,rain", []),
        ("station,lat,lon,date,rain,snow", ["--names", "station,lat,lon,date,rain,rain"]),
    ],
)
def test_read_refuses_repeated_headers(tmp_path, csv_io, header, extra):
    src = _write(tmp_path, f"{header}\nA,1,36,2026-10-01,1,2\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "out.zarr"), *extra)


def test_unpivot_refuses_repeated_level_headers(tmp_path, csv_io):
    src = _write(tmp_path, "lat,lon,850,850,500\n1,36,1,2,3\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"), "--unpivot", "level=t")


def test_repeated_index_exits_2(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm]\nA,1,36,2026-10-01,1\n")
    _exits_2(
        csv_io,
        "--csv",
        str(src),
        "-o",
        str(tmp_path / "o.zarr"),
        "--index",
        "station",
        "--index",
        "station_id",
        "--index",
        "date",
    )


def test_read_utf16_with_blank_lines_above_the_header(tmp_path, csv_io):
    src = tmp_path / "in.txt"
    src.write_bytes(
        "\n\nstation\tlat\tlon\tdate\train [mm]\nA\t1\t36\t2026-10-01\t1.5\n".encode("utf-16")
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    np.testing.assert_allclose(_open(out)["rain"].values, [[1.5]])


def test_time_header_notes_are_not_time_zones(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,Date (dd/mm/yyyy),rain [mm]\nA,1,36,01/10/2026,1\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--date-format", "%d/%m/%Y")
    assert _open(out)["time"].values[0] == np.datetime64("2026-10-01")


def test_date_format_without_a_year_exits_2(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm]\nA,1,36,Oct-15,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"), "--date-format", "%b-%d")


def test_unreadable_units_exit_2(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm/dayy]\nA,1,36,2026-10-01,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


def test_text_steps_exit_2(tmp_path, csv_io):
    src = _write(tmp_path, "time,step [days],lat,lon,tp [mm]\n2026-10-01,1 days,1,36,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


def test_empty_cell_in_a_calendar_time_column_exits_2(tmp_path, csv_io):
    src = _write(tmp_path, "time [noleap],lat,lon,rain [mm]\n2026-02-01,1,36,1\n,1,36,2\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


@pytest.mark.parametrize("spec", ["time=time", "time=lat", "time=station", "precip=precip"])
def test_unpivot_names_must_not_clash(tmp_path, csv_io, spec):
    src = _write(tmp_path, "station,lat,lon,2026-10-15,2026-10-16\nA,1,36,1,2\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"), "--unpivot", spec)


def test_unpivot_stacks_a_text_column_that_holds_numbers(tmp_path, csv_io):
    # "tr" beside a number is an unmapped code, not a label column.
    src = _write(tmp_path, "station,lat,lon,d15,d16\nA,0,36,tr,1\nB,1,37,2,4\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"), "--unpivot", "day=precip")


def test_unpivot_refuses_headers_mixing_numbers_and_text(tmp_path, csv_io):
    src = _write(tmp_path, "lat,lon,850,500,Total\n1,36,1,2,3\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"), "--unpivot", "level=t")


def test_drop_column_takes_the_header_as_written(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,Total Rain (mm),2026-10-15\nA,1,36,9,1\n")
    out = tmp_path / "out.zarr"
    run_skill(
        csv_io,
        "--csv",
        str(src),
        "-o",
        str(out),
        "--drop-column",
        "Total Rain (mm)",
        "--unpivot",
        "time=precip",
    )
    np.testing.assert_allclose(_open(out)["precip"].values, [[1.0]])


def test_excel_skips_hidden_sheets(tmp_path, csv_io):
    from openpyxl import Workbook

    wb = Workbook()
    stale = wb.active
    stale.title = "old"
    stale.append(["station", "lat", "lon", "date", "rain [mm]"])
    stale.append(["A", 1, 36, "2026-10-01", 100])
    stale.sheet_state = "hidden"
    live = wb.create_sheet("rain")
    live.append(["station", "lat", "lon", "date", "rain [mm]"])
    live.append(["A", 1, 36, "2026-10-01", 1])
    src = tmp_path / "in.xlsx"
    wb.save(src)
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    np.testing.assert_allclose(_open(out)["rain"].values, [[1.0]])


def test_excel_refuses_formulas_with_no_saved_value(tmp_path, csv_io):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["station", "lat", "lon", "date", "rain [mm]"])
    ws.append(["A", 1, 36, "2026-10-01", "=1+1"])
    src = tmp_path / "in.xlsx"
    wb.save(src)
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


@pytest.mark.parametrize(
    "name,body", [("in.xlsx", b"station,lat\n"), ("in.xlsx", b""), ("in.ods", b"PK")]
)
def test_refuses_files_that_are_not_workbooks(tmp_path, csv_io, name, body):
    src = tmp_path / name
    src.write_bytes(body)
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


@pytest.mark.parametrize("header", ["Time (EAT)", "Time (local)"])
def test_time_header_abbreviations_exit_2(tmp_path, csv_io, header):
    src = _write(tmp_path, f"station,lat,lon,{header},t [degC]\nA,1,36,2026-10-01 03:00,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


def test_codes_leave_coordinates_alone(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,date,level,t [degC]\nA,0,36,2026-10-01,1000,0\nA,0,36,2026-10-01,850,5\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--na-value", "0", "--na-value", "1000")
    res = _open(out)
    assert float(res["latitude"].values[0]) == 0.0
    assert sorted(res["level"].values) == [850, 1000]
    np.testing.assert_allclose(res["t"].sel(level=1000).values, [[np.nan]])


def test_names_differing_only_by_case_exit_2(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,Rain,rain\nA,1,36,2026-10-01,1,2\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


def test_blank_names_exit_2(tmp_path, csv_io):
    src = _write(tmp_path, "a,b,c,d,e,f\nA,1,36,2026-10-01,1,2\n")
    _exits_2(
        csv_io,
        "--csv",
        str(src),
        "-o",
        str(tmp_path / "o.zarr"),
        "--names",
        "station,lat,lon,date,,",
    )


def test_drop_incomplete_with_nothing_left_exits_2(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,rain [mm]\nA,1,36,,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"), "--drop-incomplete")


def test_thousands_separator_in_a_units_column_exits_2(tmp_path, csv_io):
    src = _write(
        tmp_path, 'station,lat,lon,date,q [m3 s-1],t [degC]\nA,1,36,2026-10-01,"1,234",2\n'
    )
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"))


def test_temperature_letters_are_degrees(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,date,Tmax (C),Tmin (F)\nA,1,36,2026-10-01,25,60\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    res = _open(out)
    assert units_equal(res["Tmax"].attrs["units"], "degC")
    assert units_equal(res["Tmin"].attrs["units"], "degF")


def test_excel_skips_blank_rows_above_the_header(tmp_path, csv_io):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append([])
    ws.append([])
    ws.append(["station", "lat", "lon", "date", "rain [mm]"])
    ws.append(["A", 1, 36, "2026-10-01", 1])
    src = tmp_path / "in.xlsx"
    wb.save(src)
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    np.testing.assert_allclose(_open(out)["rain"].values, [[1.0]])


def test_matching_timezone_and_header_agree(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,time [UTC],t [degC]\nA,1,36,2026-10-01 03:00,1\n")
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--timezone", "+00:00")
    assert _open(out)["time"].values[0] == np.datetime64("2026-10-01T03:00")


def test_excel_formulas_outside_the_table_are_ignored(tmp_path, csv_io):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["=TODAY()"])
    ws.append(["station", "lat", "lon", "date", "rain [mm]", "check"])
    ws.append(["A", 1, 36, "2026-10-01", 1, "=E3*2"])
    src = tmp_path / "in.xlsx"
    wb.save(src)
    out = tmp_path / "out.zarr"
    run_skill(
        csv_io, "--csv", str(src), "-o", str(out), "--skip-rows", "1", "--drop-column", "check"
    )
    np.testing.assert_allclose(_open(out)["rain"].values, [[1.0]])


def test_measured_heights_stay_variables(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,date,snow_height [cm],rain [mm]\nA,1,36,2026-10-01,5,1\nA,1,36,2026-10-02,5,2\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    assert _open(out)["snow_height"].dims == ("station_id", "time")


def test_unpivot_value_must_not_name_a_stacked_column(tmp_path, csv_io):
    src = _write(tmp_path, "station,lat,lon,rain,2026-10-15\nA,1,36,9,1\n")
    _exits_2(csv_io, "--csv", str(src), "-o", str(tmp_path / "o.zarr"), "--unpivot", "time=rain")


def test_drop_column_takes_an_excel_date_header(tmp_path, csv_io):
    src = tmp_path / "in.xlsx"
    df = pd.DataFrame([[36.8, -1.3, "A", 1.0, 2.0]])
    df.columns = ["lon", "lat", "station", pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")]
    df.to_excel(src, index=False)
    out = tmp_path / "out.zarr"
    run_skill(
        csv_io,
        "--csv",
        str(src),
        "-o",
        str(out),
        "--drop-column",
        "2026-10-16",
        "--unpivot",
        "time=precip",
    )
    assert len(_open(out)["time"]) == 1


def test_float32_with_nan_exports_and_round_trips(tmp_path, csv_io):
    ds = make_gridded(n_time=1, lats=(1.0,), lons=(10.0, 11.0))
    ds["precip"] = ds["precip"].astype(np.float32)
    ds["precip"].values[0, 0, 0] = np.nan
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"
    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))
    np.testing.assert_allclose(_open(back)["precip"].values, [[[np.nan, 1.0]]])


def test_pivot_of_hourly_steps_reads_back_in_hours(tmp_path, csv_io, capsys):
    ds = make_forecast(n_step=3)
    ds = ds.assign_coords(step=pd.to_timedelta([6, 12, 18], unit="h"))
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"
    run_skill(csv_io, "-i", str(src), "-o", str(csv), "--pivot", "step=tp")
    assert "--units step=hours" in capsys.readouterr().err
    run_skill(
        csv_io,
        "--csv",
        str(csv),
        "-o",
        str(back),
        "--unpivot",
        "step=tp",
        "--units",
        "step=hours",
        "--units",
        "tp=mm",
    )
    assert _open(back)["step"].values[0] == np.timedelta64(6, "h")


def test_na_value_equal_to_a_real_value_exits_2(tmp_path, csv_io):
    ds = make_gridded(n_time=1, lats=(1.0,), lons=(10.0,), fill=0.0)
    src = write_zarr(ds, tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / "out.csv"), "--na-value", "0")


@pytest.mark.parametrize("name", ["level", "lat", "a [b]", "x (y)"])
def test_export_refuses_names_that_read_back_differently(tmp_path, csv_io, name):
    ds = make_gridded(n_time=1, lats=(1.0,), lons=(10.0,)).rename(precip=name)
    src = write_zarr(ds, tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / "out.csv"))


def test_sub_second_times_round_trip(tmp_path, csv_io):
    ds = make_point_obs(n_time=2, n_points=1)
    ds = ds.assign_coords(time=pd.to_datetime(["2026-10-01 00:00:00.25", "2026-10-01 00:00:01.5"]))
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"
    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))
    np.testing.assert_array_equal(_open(back)["time"].values, ds["time"].values)


def test_forecast_valid_time_is_left_out_and_reads_back(tmp_path, csv_io):
    ds = make_forecast(n_step=2)
    ds = ds.assign_coords(valid_time=("step", ds["time"].values + ds["step"].values))
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"
    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    assert "valid_time" not in pd.read_csv(csv).columns
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))
    assert _open(back)["tp"].dims == ("step", "latitude", "longitude")


def test_date_variables_round_trip(tmp_path, csv_io):
    ds = make_point_obs(n_time=1, n_points=2).isel(time=0, drop=True)
    ds["onset"] = ("point_id", pd.to_datetime(["2026-10-05", None]).values)
    ds = ds.drop_vars("precip").rename(point_id="station_id")
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"
    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    assert "onset" in pd.read_csv(csv).columns
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))
    res = _open(back)
    assert res["onset"].values[0] == np.datetime64("2026-10-05")
    assert np.isnat(res["onset"].values[1])


def test_pivot_needs_the_variable_along_the_dim(tmp_path, csv_io):
    ds = make_gridded(n_time=2, lats=(1.0,), lons=(10.0,))
    ds["elev"] = (("latitude", "longitude"), [[100.0]])
    src = write_zarr(ds, tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / "o.csv"), "--pivot", "time=elev")


def test_text_that_looks_like_a_formula_stays_text(tmp_path, csv_io):
    ds = make_point_obs(n_time=1, n_points=1).rename(point_id="station_id")
    ds = ds.assign_coords(name=("station_id", ["=1+1"]))
    src = write_zarr(ds, tmp_path / "in.zarr")
    for ext in ("csv", "xlsx"):
        out = tmp_path / f"out.{ext}"
        back = tmp_path / f"back_{ext}.zarr"
        run_skill(csv_io, "-i", str(src), "-o", str(out))
        run_skill(csv_io, "--csv", str(out), "-o", str(back))
        assert list(_open(back)["name"].values) == ["=1+1"]


@pytest.mark.parametrize("output", ["out.xlsm", "folder.csv"])
def test_export_refuses_bad_outputs(tmp_path, csv_io, output):
    (tmp_path / "folder.csv").mkdir()
    src = write_zarr(make_gridded(n_time=1), tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / output))


def test_excel_drops_control_characters(tmp_path, csv_io):
    ds = make_point_obs(n_time=1, n_points=1).rename(point_id="station_id")
    ds = ds.assign_coords(name=("station_id", ["Kitui\x01"]))
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.xlsx"
    run_skill(csv_io, "-i", str(src), "-o", str(out))
    assert pd.read_excel(out)["name"].iloc[0] == "Kitui"


@pytest.mark.parametrize("header", ["Station Name (official)", "QC (flag)", "remarks [-]"])
def test_label_columns_with_bracketed_notes_still_read(tmp_path, csv_io, header):
    src = _write(
        tmp_path, f"station,lat,lon,date,{header},rain [mm]\nA,1,36,2026-10-01,Dagoretti,1\n"
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    np.testing.assert_allclose(_open(out)["rain"].values, [[1.0]])


def test_date_columns_mixing_offsets_and_zone_headers(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,lat,lon,date,onset [Africa/Nairobi],seen,rain [mm]\n"
        "A,1,36,2026-10-01,2026-10-15,2026-10-01T00:00Z,1\n"
        "B,2,37,2026-10-01,2026-10-16,2026-10-01T03:00+03:00,2\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out))
    res = _open(out)
    assert list(res["onset"].values) == [np.datetime64("2026-10-15"), np.datetime64("2026-10-16")]
    assert res["onset"].dims == ("station_id",)


def test_valid_time_without_time_becomes_the_init(tmp_path, csv_io):
    ds = make_forecast(n_step=2)
    init = ds["time"].values
    ds = ds.drop_vars("time").assign_coords(valid_time=("step", init + ds["step"].values))
    src = write_zarr(ds, tmp_path / "in.zarr")
    csv = tmp_path / "out.csv"
    back = tmp_path / "back.zarr"
    run_skill(csv_io, "-i", str(src), "-o", str(csv))
    run_skill(csv_io, "--csv", str(csv), "-o", str(back))
    assert _open(back)["time"].values == init


def test_date_format_that_drops_the_hour_exits_2(tmp_path, csv_io):
    # Daily values stamped 06:00: distinct dates, but %d/%m/%Y would lose the hour.
    ds = make_gridded(n_time=2, lats=(1.0,), lons=(10.0,))
    ds = ds.assign_coords(time=pd.date_range("2026-10-01 06:00", periods=2, freq="D"))
    src = write_zarr(ds, tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / "o.csv"), "--date-format", "%d/%m/%Y")


def test_nanosecond_times_exit_2(tmp_path, csv_io):
    ds = make_gridded(n_time=2, lats=(1.0,), lons=(10.0,))
    ds = ds.assign_coords(time=ds["time"].values + np.timedelta64(5, "ns"))
    src = write_zarr(ds, tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / "o.csv"))


def test_export_refuses_a_coord_that_reads_back_as_another(tmp_path, csv_io):
    ds = make_point_obs(n_time=1, n_points=1).rename(point_id="station_id")
    ds = ds.assign_coords(lat=("station_id", [1.0]))
    src = write_zarr(ds, tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / "o.csv"))


def test_export_refuses_missing_time_labels(tmp_path, csv_io):
    ds = make_gridded(n_time=2, lats=(1.0,), lons=(10.0,))
    ds = ds.assign_coords(time=[np.datetime64("2026-10-01", "ns"), np.datetime64("NaT", "ns")])
    src = write_zarr(ds, tmp_path / "in.zarr")
    _exits_2(csv_io, "-i", str(src), "-o", str(tmp_path / "o.csv"))


def test_text_code_leaves_a_name_column_alone(tmp_path, csv_io):
    src = _write(
        tmp_path,
        "station,name,lat,lon,date,rain [mm]\nA,M,1,36,2026-10-01,M\nA,M,1,36,2026-10-02,2\n",
    )
    out = tmp_path / "out.zarr"
    run_skill(csv_io, "--csv", str(src), "-o", str(out), "--na-value", "M")
    res = _open(out)
    assert list(res["name"].values) == ["M"]
    np.testing.assert_allclose(res["rain"].values, [[np.nan, 2.0]])

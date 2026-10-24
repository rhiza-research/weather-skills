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
    assert "rain ('T')" in capsys.readouterr().err


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

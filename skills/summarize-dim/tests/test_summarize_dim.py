"""Correctness tests for summarize-dim."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def summarize_dim():
    return load_skill("summarize-dim", "summarize_dim").summarize_dim


def test_mean_collapses_time(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(n_time=3, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert ds["precip"].values == pytest.approx(2.0)
    assert load_history(out)[-1]["skill"] == "summarize-dim"


def test_sum_of_dimensionless_flags_collapses_longitude(tmp_path, summarize_dim):
    ds = make_gridded(name="wet")
    ds["wet"].attrs.update(units="1", standard_name="")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim, "-i", str(src), "-o", str(out), "--dim", "longitude", "--method", "sum"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "longitude" not in ds.dims
    assert ds["wet"].sizes["latitude"] == 3
    assert (ds["wet"].values == 4.0).all()  # four wet cells per row


@pytest.mark.parametrize(
    "args",
    [
        ("--dim", "latitude", "--dim", "longitude"),
        ("--dim", "longitude"),
    ],
)
def test_sum_over_space_of_physical_units_is_refused(tmp_path, summarize_dim, capsys, args):
    ds = make_gridded()
    ds["precip"].attrs.update(units="mm", standard_name="", cell_methods="time: sum")
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            *args,
            "--method",
            "sum",
        )
    assert exc.value.code == 2
    assert "use --method mean" in capsys.readouterr().err


def _celsius(lats=(0.0, 60.0), lons=(10.0, 11.0)):
    import numpy as np

    ds = make_gridded(name="t2m", lats=lats, lons=lons)
    # 10 °C at the equator row, 30 °C at 60°N (cos weights 1 and 0.5).
    ds["t2m"].values[:] = np.array([10.0, 30.0])[None, :, None]
    ds["t2m"].attrs.update(units="degree_Celsius", standard_name="air_temperature")
    return ds


def test_lat_weighted_mean_celsius(tmp_path, summarize_dim):
    src = write_zarr(_celsius(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "latitude",
        "--method",
        "mean",
        "--lat-weighted",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "latitude" not in ds.dims
    # (1 * 10 + 0.5 * 30) / 1.5 = 16.67 °C — not 290 K, and not 16.67 K.
    assert ds["t2m"].values == pytest.approx(50.0 / 3.0)
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
    assert ds["t2m"].attrs["standard_name"] == "air_temperature"


def test_lat_weighted_mean_celsius_over_lat_and_lon(tmp_path, summarize_dim):
    src = write_zarr(_celsius(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "latitude",
        "--dim",
        "longitude",
        "--method",
        "mean",
        "--lat-weighted",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["t2m"].dims == ("time",)
    assert ds["t2m"].values == pytest.approx(50.0 / 3.0)


def test_lat_weighted_mean_precip_unchanged(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "latitude",
        "--method",
        "mean",
        "--lat-weighted",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].values == pytest.approx(2.0)
    assert units_equal(ds["precip"].attrs["units"], "mm day-1")


def test_accepts_precip_totals(tmp_path, summarize_dim):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    result = xr.open_zarr(out, consolidated=True)
    assert "time" not in result.dims
    assert "sum" in result["precip"].attrs["cell_methods"]


def _labeled(n_time=4):
    import numpy as np

    ds = make_gridded(n_time=n_time, lats=(1.0, 2.0), lons=(10.0, 11.0, 12.0))
    ds["precip"].values[:] = np.arange(n_time * 6, dtype=float).reshape(n_time, 2, 3)
    regions = np.array([["a", "a", ""], ["b", "b", "b"]])
    return ds.assign_coords(
        region=(("latitude", "longitude"), regions),
        region_name=(("latitude", "longitude"), np.char.upper(regions)),
        half=("time", ["x", "x", "y", "y"][:n_time]),
    )


def test_by_label_collapses_its_dims(tmp_path, summarize_dim):
    src = write_zarr(_labeled(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--by", "region", "--method", "mean")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].dims == ("time", "region")
    assert ds["region"].values.tolist() == ["a", "b"]  # "" dropped
    # time 0: a = mean(0, 1), b = mean(3, 4, 5)
    assert ds["precip"].isel(time=0).values.tolist() == [0.5, 4.0]
    assert "latitude" not in ds.dims and "region_name" not in ds.coords
    assert "(grouped by region)" in ds["precip"].attrs["cell_methods"]


def test_by_two_labels_with_extra_dim(tmp_path, summarize_dim):
    ds = _labeled().drop_vars("half")
    ds = ds.assign_coords(dayofyear=("time", ds["time"].dt.dayofyear.values))
    ds["precip"].attrs.update(units="mm", cell_methods="time: sum")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--by",
        "region",
        "--by",
        "dayofyear",
        "--method",
        "mean",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert set(ds["precip"].dims) == {"region", "dayofyear"}
    # region a, day 2: cells 0, 1 at time 1 -> mean of 6 and 7
    assert ds["precip"].sel(region="a", dayofyear=2).item() == 6.5


def test_sum_by_spatial_label_is_refused(tmp_path, summarize_dim, capsys):
    ds = _labeled().drop_vars("half")
    ds["precip"].attrs.update(units="mm", cell_methods="time: sum")
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--by",
            "region",
            "--method",
            "sum",
        )
    assert exc.value.code == 2
    assert "(via --by region)" in capsys.readouterr().err


def test_by_rejects_dim_coord(tmp_path, summarize_dim, capsys):
    src = write_zarr(_labeled(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--by",
            "time",
            "--method",
            "mean",
        )
    assert "not a label coord" in capsys.readouterr().err


def test_by_all_empty_labels_is_an_error(tmp_path, summarize_dim, capsys):
    import numpy as np

    ds = _labeled().assign_coords(region=(("latitude", "longitude"), np.full((2, 3), "")))
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--by",
            "region",
            "--method",
            "mean",
        )
    assert "empty label" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ("--by", "region", "--method", "mean"),
        ("--dim", "latitude", "--dim", "longitude", "--method", "mean", "--lat-weighted"),
    ],
)
def test_units_text_is_kept(tmp_path, summarize_dim, args):
    """Grouped and lat-weighted results keep the input's units spelling (mm day-1)."""
    src = write_zarr(_labeled(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), *args)

    assert xr.open_zarr(out, consolidated=True)["precip"].attrs["units"] == "mm day-1"


def test_by_labels_sharing_some_dims_is_an_error(tmp_path, summarize_dim, capsys):
    ds = _labeled().assign_coords(band=("latitude", ["s", "n"]))
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--by",
            "region",
            "--by",
            "band",
            "--method",
            "mean",
        )
    assert "share some but not all dims" in capsys.readouterr().err


def test_by_same_dims_combination_without_cells_is_nan(tmp_path, summarize_dim):
    """Two labels on the same dims: a pair that never co-occurs is NaN, not dropped."""
    import numpy as np

    zone = np.array([["w", "e", "e"], ["w", "w", "e"]])
    ds = _labeled().assign_coords(zone=(("latitude", "longitude"), zone))
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--by",
        "region",
        "--by",
        "zone",
        "--method",
        "max",
    )

    r = xr.open_zarr(out, consolidated=True)["precip"].isel(time=0)
    # region a = cells (0,0) w, (0,1) e; region b = (1,0) w, (1,1) w, (1,2) e
    assert r.sel(region="a", zone="w").item() == 0.0
    assert r.sel(region="a", zone="e").item() == 1.0
    assert r.sel(region="b", zone="e").item() == 5.0
    assert r.sel(region="b", zone="w").item() == 4.0


def _seasons(n_time=275, start="2020-04-01"):
    """Two stations of daily totals with year and MAM / OND labels; S1 misses 10 OND days."""
    import numpy as np

    ds = make_point_obs(n_time=n_time, start=start, fill=1.0)
    ds["precip"].attrs.update(
        units="mm", standard_name="lwe_thickness_of_precipitation_amount", cell_methods="time: sum"
    )
    month = ds["time"].dt.month.values
    ds["precip"].values[1, np.nonzero(month == 11)[0][:10]] = np.nan
    group = np.where(np.isin(month, [3, 4, 5]), "MAM", np.where(month >= 10, "OND", ""))
    ds["elevation"] = ("point_id", [10.0, 20.0])
    return ds.assign_coords(year=("time", ds["time"].dt.year.values), month_group=("time", group))


def _by_season(summarize_dim, tmp_path, *extra):
    src = write_zarr(_seasons(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--by",
        "year",
        "--by",
        "month_group",
        "--method",
        "sum",
        *extra,
    )
    return xr.open_zarr(out, consolidated=True)


def test_by_season_blanks_incomplete_groups(tmp_path, summarize_dim, capsys):
    """April-May is a partial MAM (61 of 92 days) and S1 misses 10 OND days: both blanked."""
    import numpy as np

    ds = _by_season(summarize_dim, tmp_path)

    precip = ds["precip"].sel(year=2020)  # integer label
    assert precip.dims == ("point_id", "month_group")
    assert precip.sel(point_id="S0", month_group="OND").item() == 92.0
    assert np.isnan(precip.sel(month_group="MAM")).all()
    assert np.isnan(precip.sel(point_id="S1", month_group="OND").item())
    assert "left out 3 cell-group(s)" in capsys.readouterr().err
    assert "precip_coverage" not in ds
    # A variable without the label dims passes through.
    assert ds["elevation"].values.tolist() == [10.0, 20.0]


def test_coverage_is_per_station_before_a_regional_mean(tmp_path, summarize_dim, capsys):
    """Two stations missing different days are never pooled into a complete OND."""
    import numpy as np

    ds = _seasons()
    month = ds["time"].dt.month.values
    ds["precip"].values[0, np.nonzero(month == 12)[0][:5]] = np.nan  # S0 misses Dec days
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "point_id",
        "--by",
        "year",
        "--by",
        "month_group",
        "--method",
        "mean",
    )
    result = xr.open_zarr(out, consolidated=True)["precip"].sel(year=2020, month_group="OND")
    assert np.isnan(result.item())
    assert "left out" in capsys.readouterr().err


def test_min_coverage_keeps_partial_sums_with_warning(tmp_path, summarize_dim, capsys):
    ds = _by_season(summarize_dim, tmp_path, "--min-coverage", "0.5")

    precip = ds["precip"].sel(year=2020)
    assert precip.sel(point_id="S0", month_group="MAM").item() == 61.0  # not scaled up
    assert precip.sel(point_id="S1", month_group="OND").item() == 82.0
    assert "estimates from partial data" in capsys.readouterr().err
    coverage = ds["precip_coverage"].sel(year=2020)
    assert coverage.sel(point_id="S1", month_group="MAM").item() == pytest.approx(61 / 92)
    assert coverage.sel(point_id="S1", month_group="OND").item() == pytest.approx(82 / 92)
    assert coverage.sel(point_id="S0", month_group="OND").item() == 1.0
    assert ds["precip"].attrs["ancillary_variables"] == "precip_coverage"


def test_season_crossing_december_takes_its_start_year(tmp_path, summarize_dim):
    """Dec 2020 - Feb 2021 is one complete DJF (90 days) labelled 2020."""
    import numpy as np

    ds = make_gridded(n_time=90, start="2020-12-01", lats=(1.0,), lons=(10.0,))
    ds["precip"].attrs.update(units="mm", cell_methods="time: sum")
    month = ds["time"].dt.month.values
    ds = ds.assign_coords(
        year=("time", ds["time"].dt.year.values - (month < 12)),
        month_group=("time", np.full(90, "DJF")),
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--by",
        "year",
        "--by",
        "month_group",
        "--method",
        "sum",
    )

    result = xr.open_zarr(out, consolidated=True)["precip"]
    assert result.sel(year=2020, month_group="DJF").values.ravel().tolist() == [90.0]


def test_sum_of_rates_over_time_is_refused(tmp_path, summarize_dim, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--dim",
            "time",
            "--method",
            "sum",
        )
    assert "convert the rates to period totals first" in capsys.readouterr().err


def test_min_coverage_needs_a_time_label(tmp_path, summarize_dim, capsys):
    src = write_zarr(_labeled(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--by",
            "region",
            "--method",
            "mean",
            "--min-coverage",
            "0.5",
        )
    assert "--min-coverage needs a --by label along time" in capsys.readouterr().err


def test_sum_over_stations_is_refused(tmp_path, summarize_dim, capsys):
    src = write_zarr(_seasons(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--dim",
            "point_id",
            "--method",
            "sum",
        )
    assert exc.value.code == 2
    assert "--method sum over point_id would add up" in capsys.readouterr().err


def test_by_labels_that_never_meet_is_an_error(tmp_path, summarize_dim, capsys):
    import numpy as np

    zone = np.array([["", "", "w"], ["", "", ""]])  # only on the cell outside every region
    ds = _labeled().assign_coords(zone=(("latitude", "longitude"), zone))
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--by",
            "region",
            "--by",
            "zone",
            "--method",
            "mean",
        )
    assert "nothing to group" in capsys.readouterr().err


def test_repeated_time_step_is_refused(tmp_path, summarize_dim, capsys):
    """A repeated day would count twice and pass as a complete season."""
    ds = _seasons()
    src = write_zarr(xr.concat([ds, ds.isel(time=[200])], "time"), tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--by",
            "year",
            "--by",
            "month_group",
            "--method",
            "sum",
        )
    assert "appears more than once" in capsys.readouterr().err


def _weekly(start):
    """Weekly mm totals for 2020 labelled like label-group --time year --time MAM,OND."""
    import numpy as np

    ds = make_point_obs(n_time=1, n_points=1)
    times = xr.date_range(start, "2020-12-31", freq="7D")
    month = times.month
    group = np.where(month.isin([3, 4, 5]), "MAM", np.where(month >= 10, "OND", ""))
    return xr.Dataset(
        {"precip": (("point_id", "time"), np.ones((1, times.size)))},
        coords={"point_id": ds["point_id"].values, "time": times},
    ).assign_coords(year=("time", times.year), month_group=("time", group))


def test_weekly_steps_crossing_a_season_are_refused(tmp_path, summarize_dim, capsys):
    """A week from 27 May takes in June days, so its MAM total would not be MAM's."""
    ds = _weekly("2020-01-01")
    ds["precip"].attrs.update(units="mm", aggregation_period="7 day")
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--by",
            "year",
            "--by",
            "month_group",
            "--method",
            "sum",
        )
    assert "runs into the next --by" in capsys.readouterr().err


def test_daily_steps_stamped_at_noon_are_kept(tmp_path, summarize_dim):
    """A daily step belongs to the day it is stamped on, whatever the hour."""
    import pandas as pd

    ds = _seasons()
    ds = ds.assign_coords(time=ds.indexes["time"] + pd.Timedelta("12h"))
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--by",
        "year",
        "--by",
        "month_group",
        "--method",
        "sum",
    )
    precip = xr.open_zarr(out, consolidated=True)["precip"]
    assert precip.sel(point_id="S0", year=2020, month_group="OND").item() == 92.0

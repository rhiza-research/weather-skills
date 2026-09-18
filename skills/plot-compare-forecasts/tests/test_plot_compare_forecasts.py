"""Correctness tests for plot-compare-forecasts."""

import json
from pathlib import Path

import numpy as np
import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core import DataError
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_mod():
    return load_skill("plot-compare-forecasts", "plot_compare_forecasts")


@pytest.fixture(scope="module")
def plot_fn(plot_mod):
    return plot_mod.plot_compare_forecasts


def test_two_forecasts_write_png_and_stamp_history(tmp_path, plot_fn):
    a = write_zarr(make_forecast(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_forecast(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "grid.png"

    run_skill(
        plot_fn,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--title",
        "Two forecasts",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot-compare-forecasts"
    assert history[-1]["args"]["title"] == "Two forecasts"


def test_parse_figsize(plot_mod):
    import argparse

    assert plot_mod.parse_figsize("12,8") == (12.0, 8.0)
    assert plot_mod.parse_figsize("10x6") == (10.0, 6.0)
    with pytest.raises(argparse.ArgumentTypeError, match="W,H"):
        plot_mod.parse_figsize("wide")


def test_figsize_writes_png(tmp_path, plot_fn):
    a = write_zarr(make_forecast(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_forecast(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "grid.png"

    run_skill(
        plot_fn,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--figsize",
        "11,7",
    )

    assert Path(out).exists()
    import matplotlib.image as mpimg

    img = mpimg.imread(out)
    assert img.shape[1] == 11 * 150
    assert img.shape[0] == 7 * 150
    history = load_figure_history(out)
    assert history[-1]["args"]["figsize"] == [11.0, 7.0]


def test_shorter_horizon_blank_does_not_crash(tmp_path, plot_fn, plot_mod):
    long = make_forecast(n_step=3, fill=1.0)
    short = make_forecast(n_step=2, fill=2.0)
    columns, matches, _w, _steps, _dims = plot_mod.align_valid_times([long, short])
    assert len(columns) == 3
    assert matches[0] == [0, 1, 2]
    assert matches[1] == [0, 1, None]

    a = write_zarr(long, tmp_path / "long.zarr")
    b = write_zarr(short, tmp_path / "short.zarr")
    out = tmp_path / "grid.png"
    run_skill(plot_fn, "-i", str(a), "-i", str(b), "-o", str(out))
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_different_inits_are_different_valid_time_columns(plot_mod):
    a = make_forecast(n_step=3, init="2026-01-01")
    b = make_forecast(n_step=3, init="2026-01-02")
    columns, matches, _w, _steps, _dims = plot_mod.align_valid_times([a, b])
    assert len(columns) == 4
    dates = [
        np.datetime_as_string(np.asarray(t).astype("datetime64[D]"), unit="D") for t in columns
    ]
    assert dates == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    assert matches[0] == [0, 1, 2, None]
    assert matches[1] == [None, 0, 1, 2]


def test_resolution_mismatch_errors(plot_mod):
    daily = make_forecast(n_step=3)
    weekly = make_forecast(n_step=3).assign_coords(
        step=("step", np.array([np.timedelta64(d, "D") for d in (7, 14, 21)]))
    )
    with pytest.raises(DataError, match="different time resolutions"):
        plot_mod.align_valid_times([daily, weekly])


def test_bbox_slices_before_draw(tmp_path, plot_fn):
    a = write_zarr(make_forecast(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_forecast(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "grid.png"
    run_skill(
        plot_fn,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--bbox",
        "3/9/0/12",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_two_obs_time_cubes_write_png(tmp_path, plot_fn, plot_mod):
    a = make_gridded(n_time=3, fill=1.0, start="2026-01-01")
    b = make_gridded(n_time=2, fill=2.0, start="2026-01-01")
    columns, matches, _w, _steps, dims = plot_mod.align_valid_times([a, b])
    assert dims == ["time", "time"]
    assert len(columns) == 3
    assert matches[0] == [0, 1, 2]
    assert matches[1] == [0, 1, None]

    out = tmp_path / "grid.png"
    run_skill(
        plot_fn,
        "-i",
        str(write_zarr(a, tmp_path / "a.zarr")),
        "-i",
        str(write_zarr(b, tmp_path / "b.zarr")),
        "-o",
        str(out),
        "--variable",
        "precip",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_forecast_and_obs_share_valid_times(tmp_path, plot_fn, plot_mod):
    fc = make_forecast(n_step=3, fill=1.0, init="2026-01-01", name="precip")
    obs = make_gridded(n_time=2, fill=2.0, start="2026-01-01")
    columns, matches, _w, _steps, dims = plot_mod.align_valid_times([fc, obs])
    assert dims == ["step", "time"]
    dates = [
        np.datetime_as_string(np.asarray(t).astype("datetime64[D]"), unit="D") for t in columns
    ]
    assert dates == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert matches[0] == [0, 1, 2]
    assert matches[1] == [0, 1, None]

    out = tmp_path / "grid.png"
    run_skill(
        plot_fn,
        "-i",
        str(write_zarr(fc, tmp_path / "fc.zarr")),
        "-i",
        str(write_zarr(obs, tmp_path / "obs.zarr")),
        "-o",
        str(out),
        "--variable",
        "precip",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_precip_default_colormap_is_nested_week_window():
    from weather_skills_core.plot.theme import precip_nested_palette, resolve_colorscale

    da = make_forecast()["tp"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    scale = resolve_colorscale(da, None)
    week = precip_nested_palette("ppt_week")
    assert scale["name"] == "ppt_week"
    assert scale["bounds"] == pytest.approx(week["bounds"])

    da.attrs["aggregation_period"] = "1 day"
    scale_daily = resolve_colorscale(da, None)
    daily = precip_nested_palette("ppt_daily")
    assert scale_daily["name"] == "ppt_daily"
    assert scale_daily["bounds"] == pytest.approx(daily["bounds"])

    t2m = make_gridded(name="t2m")["t2m"]
    t2m.attrs.update(units="degree_Celsius", standard_name="air_temperature")
    scale_t = resolve_colorscale(t2m, None)
    assert scale_t["name"] == "rocket"
    assert scale_t.get("bounds") is None


def test_precip_anomaly_colormap_is_chirps_palette():
    from weather_skills_core.plot.theme import PRECIP_ANOMALY_BOUNDS, resolve_colorscale

    da = make_gridded(fill=-25.0)["precip"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    scale = resolve_colorscale(da, None)
    assert scale["name"] == "chirps_anom"
    assert scale["bounds"] == pytest.approx(PRECIP_ANOMALY_BOUNDS)


def test_heatmap_scale_stretch_drops_precip_boundary_norm():
    from weather_skills_core.plot.theme import resolve_colorscale

    da = make_forecast()["tp"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    scale = resolve_colorscale(da, None, stretch=True)
    assert scale.get("bounds") is None
    assert scale["name"] == "ppt_week"


def test_vmin_vmax_writes_png_and_stamps_history(tmp_path, plot_fn):
    a = write_zarr(make_forecast(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_forecast(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "grid.png"

    run_skill(
        plot_fn,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--vmin",
        "0",
        "--vmax",
        "5",
    )

    assert Path(out).exists()
    history = load_figure_history(out)
    assert history[-1]["args"]["vmin"] == 0.0
    assert history[-1]["args"]["vmax"] == 5.0


def test_replot_from_spec(tmp_path, plot_fn):
    a = write_zarr(make_forecast(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_forecast(fill=2.0), tmp_path / "b.zarr")
    first = tmp_path / "grid.png"
    run_skill(
        plot_fn,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(first),
        "--title",
        "Original",
    )
    spec_path = tmp_path / "grid.plot.json"
    data = json.loads(spec_path.read_text())
    data["title"] = "Edited"
    spec_path.write_text(json.dumps(data))
    second = tmp_path / "grid2.png"
    run_skill(plot_fn, "--spec", str(spec_path), "-o", str(second))
    assert second.is_file() and second.stat().st_size > 0
    history = load_figure_history(second)
    assert history[-1]["skill"] == "plot-compare-forecasts"
    basenames = [item["basename"] for item in history[-1]["input"]]
    assert "a.zarr" in basenames and "b.zarr" in basenames

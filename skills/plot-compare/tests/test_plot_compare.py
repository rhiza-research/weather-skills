"""Correctness tests for plot-compare."""

import json
from pathlib import Path

import pytest
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_compare():
    return load_skill("plot-compare", "plot_compare").plot_compare


def test_two_gridded_inputs_write_png(tmp_path, plot_compare):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "cmp.png"

    run_skill(
        plot_compare,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--panels",
        "2",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_parse_figsize():
    import argparse

    plot_mod = load_skill("plot-compare", "plot_compare")
    assert plot_mod.parse_figsize("16,8") == (16.0, 8.0)
    with pytest.raises(argparse.ArgumentTypeError, match="W,H"):
        plot_mod.parse_figsize("wide")


def test_figsize_writes_png(tmp_path, plot_compare):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "cmp.png"

    run_skill(
        plot_compare,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--panels",
        "2",
        "--figsize",
        "12,6",
    )

    assert Path(out).exists()
    import matplotlib.image as mpimg

    img = mpimg.imread(out)
    assert img.shape[1] == 12 * 150
    assert img.shape[0] == 6 * 150
    history = load_figure_history(out)
    assert history[-1]["args"]["figsize"] == [12.0, 6.0]


def test_precip_shared_scale_is_nested_week_window():
    from weather_skills_core.plot.theme import precip_nested_palette, resolve_colorscale

    da = make_gridded(fill=8.0)["precip"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    scale = resolve_colorscale(da, None)
    week = precip_nested_palette("ppt_week")
    assert scale["name"] == "ppt_week"
    assert scale["bounds"] == pytest.approx(week["bounds"])


def test_precip_anomaly_row_scale_is_chirps_palette():
    from weather_skills_core.plot.theme import PRECIP_ANOMALY_BOUNDS, resolve_colorscale

    da = make_gridded(fill=-40.0)["precip"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    scale = resolve_colorscale(da, None)
    assert scale["name"] == "chirps_anom"
    assert scale["bounds"] == pytest.approx(PRECIP_ANOMALY_BOUNDS)


def test_parse_colormap_accepts_comma_separated_colors():
    from weather_skills_core.plot.theme import parse_colormap_spec

    assert parse_colormap_spec(None) == {}
    assert parse_colormap_spec("magma") == {"name": "magma"}
    parsed = parse_colormap_spec("white,wheat,green")
    assert parsed["name"] == "custom"
    assert parsed["colors"] == ["white", "wheat", "green"]


def test_custom_color_list_writes_png(tmp_path, plot_compare):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "cmp.png"

    run_skill(
        plot_compare,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--panels",
        "2",
        "--colormap",
        "white,wheat,green",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_row_scale_vmin_vmax_drops_precip_boundary_norm():
    from weather_skills_core.plot.maps import scale_from_da

    da = make_gridded(fill=8.0)["precip"]
    da.attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    scale = scale_from_da(da, None, stretch=True, vmin=0.0, vmax=25.0)
    assert scale.get("bounds") is None
    assert scale["cmin"] == 0.0
    assert scale["cmax"] == 25.0
    assert scale["colors"]


def test_vmin_vmax_writes_png_and_stamps_history(tmp_path, plot_compare):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "cmp.png"

    run_skill(
        plot_compare,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--panels",
        "2",
        "--vmin",
        "0",
        "--vmax",
        "10",
    )

    assert Path(out).exists()
    history = load_figure_history(out)
    assert history[-1]["args"]["vmin"] == 0.0
    assert history[-1]["args"]["vmax"] == 10.0


def test_replot_from_spec(tmp_path, plot_compare):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    first = tmp_path / "cmp.png"
    run_skill(
        plot_compare,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(first),
        "--panels",
        "2",
        "--title",
        "Original",
    )
    spec_path = tmp_path / "cmp.plot.json"
    data = json.loads(spec_path.read_text())
    data["title"] = "Edited"
    spec_path.write_text(json.dumps(data))
    second = tmp_path / "cmp2.png"
    run_skill(plot_compare, "--spec", str(spec_path), "-o", str(second))
    assert second.is_file() and second.stat().st_size > 0
    history = load_figure_history(second)
    assert history[-1]["skill"] == "plot-compare"
    basenames = [item["basename"] for item in history[-1]["input"]]
    assert "a.zarr" in basenames and "b.zarr" in basenames

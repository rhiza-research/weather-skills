"""Correctness tests for plot-mediogram."""

from pathlib import Path

import pytest
from conftest import load_skill, make_forecast, run_skill, write_zarr
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_mediogram():
    return load_skill("plot-mediogram", "plot_mediogram").plot_mediogram


def _ensemble_rate_forecast(**kwargs):
    """Forecast cube with rate units so to_standard_units can normalize both inputs."""
    ds = make_forecast(name="precip", **kwargs)
    ds["precip"].attrs.update(
        units="mm day-1",
        standard_name="lwe_precipitation_rate",
        long_name="Precipitation rate",
    )
    return ds


def test_ensemble_forecast_vs_mclimate(tmp_path, plot_mediogram):
    fc = write_zarr(_ensemble_rate_forecast(members=5, n_step=4), tmp_path / "fc.zarr")
    mc = write_zarr(_ensemble_rate_forecast(members=5, n_step=4, fill=0.5), tmp_path / "mc.zarr")
    out = tmp_path / "medio.png"

    run_skill(
        plot_mediogram,
        "-i",
        str(fc),
        "-i",
        str(mc),
        "-o",
        str(out),
        "--lat",
        "1.0",
        "--lon",
        "10.0",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_parse_figsize():
    import argparse

    plot_mod = load_skill("plot-mediogram", "plot_mediogram")
    assert plot_mod.parse_figsize("12,6") == (12.0, 6.0)
    with pytest.raises(argparse.ArgumentTypeError, match="W,H"):
        plot_mod.parse_figsize("wide")


def test_figsize_writes_png(tmp_path, plot_mediogram):
    fc = write_zarr(_ensemble_rate_forecast(members=5, n_step=4), tmp_path / "fc.zarr")
    mc = write_zarr(_ensemble_rate_forecast(members=5, n_step=4, fill=0.5), tmp_path / "mc.zarr")
    out = tmp_path / "medio.png"

    run_skill(
        plot_mediogram,
        "-i",
        str(fc),
        "-i",
        str(mc),
        "-o",
        str(out),
        "--lat",
        "1.0",
        "--lon",
        "10.0",
        "--figsize",
        "8x4",
    )

    assert Path(out).exists()
    history = load_figure_history(out)
    assert history[-1]["args"]["figsize"] == [8.0, 4.0]

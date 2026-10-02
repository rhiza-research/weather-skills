"""Correctness tests for oisst-fetch (mocked network)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("oisst-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _oisst_piece():
    ds = make_gridded(n_time=2, start="2026-01-01", name="sst", fill=295.0)
    ds = ds.rename({"latitude": "lat", "longitude": "lon"})
    ds["sst"].attrs.update(units="K", long_name="Sea Surface Temperature")
    return ds


def _html_error_dataset():
    """What --probe-latest sees when NOAA PSL serves a 502 HTML page."""
    return xr.Dataset({"time": ("time", np.array([0, 1, 2]))})


def test_fetch_writes_zarr_with_mocked_opendap(tmp_path, fetch):
    out = tmp_path / "out.zarr"

    with patch("xarray.open_dataset", return_value=_oisst_piece()):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "3/10/0/13",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "sst" in ds
    assert ds["sst"].attrs["units"] == "degree_Celsius"
    assert ds["sst"].attrs["standard_name"] == "sea_surface_temperature"
    np.testing.assert_allclose(ds["sst"].values, 295.0 - 273.15, rtol=1e-5)
    assert ds["sst"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "oisst-fetch"


def test_probe_latest_prints_max_time(capsys, fetch):
    with patch("xarray.open_dataset", return_value=_oisst_piece()):
        run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-01-02"


def test_probe_latest_retries_transient_then_succeeds(capsys, fetch):
    with (
        patch(
            "xarray.open_dataset",
            side_effect=[Exception("502 Proxy Error"), _oisst_piece()],
        ),
        patch("time.sleep"),
    ):
        run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-01-02"


def test_probe_latest_html_error_page_exits_1(fetch):
    with (
        patch("xarray.open_dataset", return_value=_html_error_dataset()),
        patch("time.sleep"),
    ):
        with pytest.raises(SystemExit) as exc:
            run_skill(fetch, "--probe-latest")
    assert exc.value.code == 1

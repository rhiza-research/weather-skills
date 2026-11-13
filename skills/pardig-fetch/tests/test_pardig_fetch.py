"""Correctness tests for pardig-fetch (mocked GCS open)."""

from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("pardig-fetch", "fetch")


def _store():
    """Two days of 15-minute frames on a 0..360 grid, 0.001 m/h everywhere."""
    times = np.arange(
        np.datetime64("2020-04-01T00:00"),
        np.datetime64("2020-04-03T00:00"),
        np.timedelta64(15, "m"),
    ).astype("datetime64[ns]")
    lat = np.array([-1.0, -0.9, -0.8])
    lon = np.array([36.0, 36.1, 359.9])
    data = np.full((times.size, lat.size, lon.size), 0.001, dtype="float32")
    ds = xr.Dataset(
        {"precipitation": (("time", "latitude", "longitude"), data)},
        coords={"time": times, "latitude": lat, "longitude": lon},
    )
    ds["precipitation"].attrs["units"] = "m h**-1"
    return ds


def test_fetch_converts_and_clips(tmp_path, mod, capsys):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_store", return_value=_store()):
        run_skill(
            mod.fetch,
            "--start-time",
            "2020-04-02",
            "--end-time",
            "2020-04-03",
            "--bbox",
            "0/35/-2/37",
            "-o",
            str(out),
        )
    ds = xr.open_zarr(out)
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["precip"].attrs["data_interval"] == "15 minute"
    np.testing.assert_allclose(ds["precip"].values, 24.0, rtol=1e-5)  # 1 mm/h
    assert list(ds["longitude"].values) == pytest.approx([36.0, 36.1])
    # Only 2020-04-02 exists: 96 frames, missing day not filled, warned.
    assert ds.sizes["time"] == 96
    assert "WARNING" in capsys.readouterr().err
    assert load_history(out)[-1]["skill"] == "pardig-fetch"


def test_fetch_window_without_data_exits_1(tmp_path, mod):
    with patch.object(mod, "_open_store", return_value=_store()), pytest.raises(SystemExit) as exc:
        run_skill(
            mod.fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "0/35/-2/37",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 1

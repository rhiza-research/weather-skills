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


def test_bbox_crossing_zero_reads_contiguous_slices(tmp_path, fetch):
    """A 0°-crossing bbox on the 0..360 grid must not send an out-of-order lon indexer.

    The netCDF4 OPeNDAP backend (outer indexing) turns an unevenly spaced integer
    indexer into one request per column (~0.6 s each live: a 20°-wide box took
    54 s instead of 5 s). The fake backend below records the keys it is read with.
    """
    from xarray.backends import BackendArray
    from xarray.core import indexing

    lons = np.arange(0.5, 360.0, 1.0)
    data = np.broadcast_to(lons, (2, 2, lons.size)).astype("float32")
    keys = []

    class RecordingArray(BackendArray):
        shape, dtype = data.shape, data.dtype

        def __getitem__(self, key):
            return indexing.explicit_indexing_adapter(
                key, self.shape, indexing.IndexingSupport.OUTER, self._raw
            )

        def _raw(self, key):
            keys.append(key)
            return data[key]

    times = np.array(["2024-06-01", "2024-06-02"], dtype="datetime64[ns]")
    sst = xr.Variable(
        ("time", "lat", "lon"),
        indexing.LazilyIndexedArray(RecordingArray()),
        attrs={"units": "degC"},
    )
    remote = xr.Dataset({"sst": sst}, coords={"time": times, "lat": [-0.5, 0.5], "lon": lons})

    out = tmp_path / "out.zarr"
    with patch("xarray.open_dataset", return_value=remote):
        run_skill(
            fetch,
            "--start-time",
            "2024-06-01",
            "--end-time",
            "2024-06-02",
            "--bbox",
            "1/-2/-1/2",
            "-o",
            str(out),
        )

    assert keys
    for key in keys:
        assert all(isinstance(k, slice | int | np.integer) for k in key), key
    got = xr.open_zarr(out, consolidated=True)
    np.testing.assert_array_equal(got["longitude"].values, [-1.5, -0.5, 0.5, 1.5])
    np.testing.assert_allclose(got["sst"].isel(time=0, latitude=0).values, [358.5, 359.5, 0.5, 1.5])


def test_window_past_latest_reports_missing_days(tmp_path, capsys, fetch):
    """Days past the latest published day are reported on stderr, never padded."""
    out = tmp_path / "out.zarr"
    with patch("xarray.open_dataset", return_value=_oisst_piece()):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-05",
            "--bbox",
            "3/10/0/13",
            "-o",
            str(out),
        )
    err = capsys.readouterr().err
    assert "no data for 3 of 5 requested days (2026-01-03..2026-01-05)" in err
    assert xr.open_zarr(out, consolidated=True).sizes["time"] == 2


def test_unpublished_next_year_is_skipped_not_fatal(tmp_path, capsys, fetch):
    """A window running into a year whose file does not exist yet keeps the published days."""
    from datetime import UTC, datetime

    year = datetime.now(UTC).year
    piece = make_gridded(n_time=2, start=f"{year - 1}-12-30", name="sst", fill=20.0)
    piece = piece.rename({"latitude": "lat", "longitude": "lon"})
    piece["sst"].attrs.update(units="degC")

    def open_by_year(url, *a, **kw):
        if f".{year}.nc" in url:
            raise OSError(f"[Errno -90] NetCDF: file not found: '{url}'")
        return piece

    out = tmp_path / "out.zarr"
    with patch("xarray.open_dataset", side_effect=open_by_year):
        run_skill(
            fetch,
            "--start-time",
            f"{year - 1}-12-30",
            "--end-time",
            f"{year}-01-02",
            "--bbox",
            "3/10/0/13",
            "-o",
            str(out),
        )
    assert xr.open_zarr(out, consolidated=True).sizes["time"] == 2
    assert "no data for 2 of 4 requested days" in capsys.readouterr().err


def test_netcdf_access_failure_is_retried(tmp_path, fetch):
    """NOAA PSL intermittently fails opens with errno -77; the retry succeeds."""
    out = tmp_path / "out.zarr"
    access_failure = OSError("[Errno -77] NetCDF: Access failure: 'https://psl.noaa.gov/...'")
    with (
        patch("xarray.open_dataset", side_effect=[access_failure, _oisst_piece()]),
        patch("time.sleep"),
    ):
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
    assert xr.open_zarr(out, consolidated=True).sizes["time"] == 2


def test_probe_latest_rejects_ident(fetch):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--probe-latest", "final")
    assert exc.value.code == 2

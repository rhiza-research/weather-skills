"""Correctness tests for imerg-fetch (mocked network)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import DataError
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("imerg-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _raw_daily(n_time=2, start="2026-01-01", units="mm/day"):
    """Mock GES DISC daily file: precipitation on (time, lon, lat), as the real files are."""
    times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D"))
    lon = np.array([10.0, 11.0, 12.0, 13.0])
    lat = np.array([1.0, 2.0, 3.0])
    data = np.arange(n_time * lon.size * lat.size, dtype=np.float32).reshape(
        n_time, lon.size, lat.size
    )
    ds = xr.Dataset(
        {"precipitation": (("time", "lon", "lat"), data)},
        coords={"time": times.astype("datetime64[ns]"), "lon": lon, "lat": lat},
    )
    ds["precipitation"].attrs.update(units=units)
    return ds


def _run(mod, fetch, out, raw, results=None, *args):
    with (
        patch.object(mod, "_login"),
        patch(
            "earthaccess.search_data", return_value=[MagicMock()] if results is None else results
        ),
        patch("earthaccess.download", return_value=["/tmp/fake.nc4"]),
        patch("xarray.open_mfdataset", return_value=raw),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
            *args,
        )


def test_fetch_writes_zarr_with_mocked_earthaccess(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    raw = _raw_daily()
    _run(mod, fetch, out, raw)

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "precip" in ds
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert "aggregation_period" not in ds["precip"].attrs
    assert load_history(out)[-1]["skill"] == "imerg-fetch"


def test_output_is_time_lat_lon_with_values_transposed(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    raw = _raw_daily()
    _run(mod, fetch, out, raw)

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].dims == ("time", "latitude", "longitude")
    got = ds["precip"].sel(latitude=2.0, longitude=12.0).values
    want = raw["precipitation"].sel(lat=2.0, lon=12.0).values
    np.testing.assert_array_equal(got, want)


def test_output_labels_late_vs_final(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    _run(mod, fetch, out, _raw_daily(), None, "--version", "final")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.attrs["imerg_version"] == "final"
    assert "GPM_3IMERGDF" in ds.attrs["source"]
    assert "Final" in ds["precip"].attrs["long_name"]


def test_no_granules_is_clean_data_error(tmp_path, mod, fetch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(mod, fetch, tmp_path / "out.zarr", _raw_daily(), [], "--version", "final")
    assert exc.value.code == 1
    assert "--probe-latest final" in capsys.readouterr().err


def test_unexpected_source_units_refused(tmp_path, mod, fetch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(mod, fetch, tmp_path / "out.zarr", _raw_daily(units="mm"))
    assert exc.value.code == 1
    assert "units" in capsys.readouterr().err


def test_probe_latest_asks_for_newest_granule_without_window(mod, fetch, capsys):
    granule = MagicMock()
    granule.data_links.return_value = [
        "https://x/3B-DAY.MS.MRG.3IMERG.20250930-S000000-E235959.V07B.nc4"
    ]
    with (
        patch.object(mod, "_login"),
        patch("earthaccess.search_data", return_value=[granule]) as search,
    ):
        run_skill(fetch, "--probe-latest", "final")
    kwargs = search.call_args.kwargs
    assert kwargs["short_name"] == "GPM_3IMERGDF"
    assert kwargs["sort_key"] == "-start_date"
    assert "temporal" not in kwargs
    assert capsys.readouterr().out.strip() == "2025-09-30"


def test_login_without_credentials_raises_data_error(mod, tmp_path):
    env = {
        k: v for k, v in os.environ.items() if k not in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD")
    }
    # Keep a real ~/.netrc on the dev machine from satisfying the netrc strategy.
    env["NETRC"] = str(tmp_path / "missing.netrc")
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(DataError, match="Earthdata authentication failed"):
            mod._login()

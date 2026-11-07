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
    """GES DISC path: the catalog is unreachable, so Late falls back to GES DISC."""
    with (
        patch.object(mod, "_open_catalog", side_effect=OSError("offline")),
        patch.object(mod, "_login"),
        patch(
            "earthaccess.search_data", return_value=[MagicMock()] if results is None else results
        ),
        patch("earthaccess.download", return_value=["/tmp/fake.nc4"]),
        patch("xarray.open_dataset", return_value=raw),
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


def test_bbox_cuts_each_granule_and_reads_one_at_a_time(tmp_path, mod, fetch):
    """Each day is downloaded, cut to --bbox and deleted before the next one."""
    days = [_raw_daily(n_time=1, start=d) for d in ("2026-01-01", "2026-01-02")]
    calls = []

    def download(granules, local_path):
        calls.append(len(granules))
        path = Path(local_path) / f"day{len(calls)}.nc4"
        path.write_bytes(b"")
        return [str(path)]

    out = tmp_path / "out.zarr"
    with (
        patch.object(mod, "_open_catalog", side_effect=OSError("offline")),
        patch.object(mod, "_login"),
        patch("earthaccess.search_data", return_value=[MagicMock(), MagicMock()]),
        patch("earthaccess.download", side_effect=download),
        patch("xarray.open_dataset", side_effect=days),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "2.5/10.5/0.5/11.5",
            "-o",
            str(out),
        )
    assert calls == [1, 1]
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    np.testing.assert_array_equal(ds["latitude"].values, [1.0, 2.0])
    np.testing.assert_array_equal(ds["longitude"].values, [11.0])


def _catalog_ds():
    """Mock dynamical.org IMERG store: half-hourly, latitude descending, SI precip units."""
    times = np.arange(
        np.datetime64("2026-01-01T00:00"),
        np.datetime64("2026-01-03T00:00"),
        np.timedelta64(30, "m"),
    )
    shape = (times.size, 2, 2)
    return xr.Dataset(
        {
            "precipitation_surface": (
                ("time", "latitude", "longitude"),
                np.full(shape, 1e-4),
                {"units": "kg m-2 s-1"},
            ),
            "precipitation_quality_index_surface": (
                ("time", "latitude", "longitude"),
                np.full(shape, 0.8),
                {"units": "1"},
            ),
        },
        coords={"time": times, "latitude": [2.0, 1.0], "longitude": [10.0, 11.0]},
    )


def test_late_reads_catalog_half_hourly_without_login(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    with (
        patch.object(mod, "_open_catalog", return_value=_catalog_ds()) as opened,
        patch.object(mod, "_login") as login,
    ):
        run_skill(fetch, "--start-time", "2026-01-01", "--end-time", "2026-01-01", "-o", str(out))
    opened.assert_called_once_with("nasa-imerg-analysis-late")
    login.assert_not_called()
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.data_vars) == ["precip"]
    assert ds["precip"].dims == ("time", "latitude", "longitude")
    assert ds.sizes["time"] == 48
    np.testing.assert_array_equal(ds["latitude"].values, [1.0, 2.0])
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["precip"].attrs["data_interval"] == "30 minute"
    np.testing.assert_allclose(ds["precip"].values, 1e-4 * 86400)
    assert ds.attrs["weather_skills_source"] == "dynamical:nasa-imerg-analysis-late"
    assert ds.attrs["imerg_version"] == "late"


def _run_daily(tmp_path, mod, catalog, end):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_catalog", return_value=catalog):
        run_skill(
            mod.fetch, "--start-time", "2026-01-01", "--end-time", end, "--daily", "-o", str(out)
        )
    return xr.open_zarr(out, consolidated=True)


def test_daily_writes_one_mean_per_utc_day(tmp_path, mod):
    catalog = _catalog_ds()
    times = catalog["time"].values
    # Each half hour holds its day number in kg m-2 s-1 / 86400 (so 1 or 2 mm day-1).
    day = ((times - times[0]) // np.timedelta64(1, "D") + 1).astype(float)
    catalog["precipitation_surface"].values[:] = (day / 86400)[:, None, None]
    # One cell misses one half hour on day 1: NaN that day, not a 47-step mean.
    catalog["precipitation_surface"].values[5, 0, 0] = np.nan
    ds = _run_daily(tmp_path, mod, catalog, "2026-01-02")
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-01-01", "2026-01-02"]
    # Latitude ascending: catalog row 0 (lat 2) is output row 1.
    assert np.isnan(ds["precip"].values[0, 1, 0])
    np.testing.assert_allclose(ds["precip"].values[0, 0], [1, 1], rtol=1e-6)
    np.testing.assert_allclose(ds["precip"].values[1], 2, rtol=1e-6)
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["precip"].attrs["data_interval"] == "1 day"
    assert ds["precip"].attrs["aggregation_period"] == "1 day"
    assert ds["precip"].attrs["cell_methods"] == "time: mean (interval: 30 minute)"
    np.testing.assert_allclose(ds["aggregation_coverage"].values, [1, 1])


def test_daily_keeps_partial_last_day_with_its_coverage(tmp_path, mod, capsys):
    # Store ends 2026-01-02T05:30: 12 of 48 half hours on the last day.
    catalog = _catalog_ds().isel(time=slice(0, 60))
    ds = _run_daily(tmp_path, mod, catalog, "2026-01-02")
    np.testing.assert_allclose(ds["aggregation_coverage"].values, [1, 0.25])
    assert "1 of 2 day(s) are incomplete" in capsys.readouterr().err


def test_late_falls_back_to_ges_disc_when_catalog_fails(tmp_path, mod, fetch, capsys):
    out = tmp_path / "out.zarr"
    _run(mod, fetch, out, _raw_daily())
    err = capsys.readouterr().err
    assert "offline" in err and "GPM_3IMERGDL" in err and "DAILY" in err
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.attrs["weather_skills_source"] == "imerg"
    assert ds.attrs["imerg_version"] == "late"
    assert ds["precip"].attrs["data_interval"] == "1 day"


def test_late_falls_back_when_catalog_has_no_data_in_window(tmp_path, mod, fetch, capsys):
    out = tmp_path / "out.zarr"
    with (
        patch.object(mod, "_open_catalog", return_value=_catalog_ds().isel(time=slice(0, 0))),
        patch.object(mod, "_login"),
        patch("earthaccess.search_data", return_value=[MagicMock()]),
        patch("earthaccess.download", return_value=["/tmp/fake.nc4"]),
        patch("xarray.open_dataset", return_value=_raw_daily()),
    ):
        run_skill(fetch, "--start-time", "2026-01-01", "--end-time", "2026-01-02", "-o", str(out))
    assert "has no data in 2026-01-01..2026-01-02" in capsys.readouterr().err
    assert xr.open_zarr(out, consolidated=True).attrs["weather_skills_source"] == "imerg"


def test_early_has_no_fallback(tmp_path, mod, fetch, capsys):
    with (
        patch.object(mod, "_open_catalog", side_effect=OSError("offline")),
        patch("earthaccess.search_data") as search,
    ):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--version",
                "early",
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-01",
                "-o",
                str(tmp_path / "out.zarr"),
            )
    assert exc.value.code == 1
    search.assert_not_called()
    assert "nasa-imerg-analysis-early" in capsys.readouterr().err


def test_final_never_touches_catalog(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    with (
        patch.object(mod, "_open_catalog") as opened,
        patch.object(mod, "_login"),
        patch("earthaccess.search_data", return_value=[MagicMock()]),
        patch("earthaccess.download", return_value=["/tmp/fake.nc4"]),
        patch("xarray.open_dataset", return_value=_raw_daily()),
    ):
        run_skill(
            fetch,
            "--version",
            "final",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )
    opened.assert_not_called()
    assert xr.open_zarr(out, consolidated=True).attrs["imerg_version"] == "final"


def test_probe_latest_late_reads_catalog_and_flags_partial_day(mod, fetch, capsys):
    with (
        patch.object(mod, "_open_catalog", return_value=_catalog_ds()),
        patch("earthaccess.search_data") as search,
    ):
        run_skill(fetch, "--probe-latest", "late")
    search.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-01-02"
    assert "partial" in captured.err

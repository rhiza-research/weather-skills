"""Correctness tests for rfe-fetch (urllib replaced by a fake CPC server)."""

import io
import urllib.error
import zipfile
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
import xarray as xr
from affine import Affine
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

_N_LAT, _N_LON = 801, 751


@pytest.fixture(scope="module")
def fetch():
    return load_skill("rfe-fetch", "fetch").fetch


def _day_file(day, fill, west=-20.049999237060547):
    """One CPC africa_rfe.YYYYMMDD.tif.zip: north-up float32 GeoTIFF with CPC's float32 transform."""
    grid = np.full((_N_LAT, _N_LON), fill, dtype="float32")
    grid[-1, 0] = -999.0  # 40S, 20W: CPC's missing marker
    grid[0, -1] = 7.0  # 40N, 55E: marks the north-east corner
    transform = Affine(0.10000000149011612, 0.0, west, 0.0, -0.10000000149011612, 40.04999923706055)
    with rasterio.MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            width=_N_LON,
            height=_N_LAT,
            count=1,
            dtype="float32",
            crs="EPSG:4035",
            transform=transform,
        ) as dst:
            dst.write(grid, 1)
        tif = mem.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"africa_rfe.{day}.tif", tif)
    return buf.getvalue()


def _fake_cpc(files):
    """urlopen stand-in serving ``files`` ({YYYYMMDD: bytes}) and a directory listing."""

    def urlopen(url, timeout=None):
        if url.endswith("/geotiff/"):
            names = "".join(f'<a href="africa_rfe.{d}.tif.zip">' for d in files)
            return io.BytesIO(names.encode())
        day = url.rsplit(".", 3)[-3]
        if day not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return io.BytesIO(files[day])

    return patch("urllib.request.urlopen", side_effect=urlopen)


def test_fetch_decodes_grid_and_skips_missing_day(tmp_path, fetch, capsys):
    out = tmp_path / "out.zarr"
    files = {"20260101": _day_file("20260101", 2.0), "20260103": _day_file("20260103", 4.0)}
    with _fake_cpc(files):
        run_skill(fetch, "--start-time", "2026-01-01", "--end-time", "2026-01-03", "-o", str(out))

    assert "2026-01-02" in capsys.readouterr().err
    ds = xr.open_zarr(out)
    assert [str(t)[:10] for t in ds.time.values] == ["2026-01-01", "2026-01-03"]
    p = ds["precip"]
    assert p.attrs["units"] == "mm day-1"
    assert p.attrs["standard_name"] == "lwe_precipitation_rate"
    assert p.attrs["data_interval"] == "1 day"
    assert ds.latitude.size == _N_LAT and ds.longitude.size == _N_LON
    np.testing.assert_allclose(ds.latitude.values[[0, -1]], [-40.0, 40.0], atol=1e-4)
    np.testing.assert_allclose(ds.longitude.values[[0, -1]], [-20.0, 55.0], atol=1e-4)
    assert np.isnan(p.sel(latitude=-40, longitude=-20, method="nearest").values).all()
    np.testing.assert_allclose(p.sel(latitude=40, longitude=55, method="nearest").values, 7.0)
    np.testing.assert_allclose(p.isel(time=1, latitude=400, longitude=400).values, 4.0)
    assert ds.attrs["weather_skills_source"] == "noaa-cpc-rfe2"
    assert load_history(out)[-1]["skill"] == "rfe-fetch"


def test_bbox_subsets_each_day(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    with _fake_cpc({"20260101": _day_file("20260101", 1.0)}):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-01",
            "--bbox",
            "5/34/-5/42",
            "-o",
            str(out),
        )
    ds = xr.open_zarr(out)
    assert float(ds.latitude.min()) >= -5 and float(ds.latitude.max()) <= 5
    assert float(ds.longitude.min()) >= 34 and float(ds.longitude.max()) <= 42
    assert ds.latitude.size == 101 and ds.longitude.size == 81


def test_grid_change_between_days_exits_1(tmp_path, fetch):
    files = {
        "20260101": _day_file("20260101", 1.0),
        "20260102": _day_file("20260102", 1.0, west=-25.05),
    }
    with _fake_cpc(files), pytest.raises(SystemExit) as exc:
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 1


def test_no_published_day_exits_1(tmp_path, fetch):
    with _fake_cpc({}), pytest.raises(SystemExit) as exc:
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 1


def test_bbox_outside_africa_exits_1(tmp_path, fetch):
    with _fake_cpc({"20260101": _day_file("20260101", 1.0)}), pytest.raises(SystemExit) as exc:
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-01",
            "--bbox",
            "50/0/45/10",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 1


def test_start_before_2001_exits_2(tmp_path, fetch):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch,
            "--start-time",
            "2000-12-31",
            "--end-time",
            "2001-01-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 2


def test_probe_latest_prints_newest_listed_day(capsys, fetch):
    with _fake_cpc({"20261014": b"", "20261015": b"", "20010101": b""}):
        run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-10-15"

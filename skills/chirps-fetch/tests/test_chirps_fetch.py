"""Correctness tests for chirps-fetch (mocked catalog)."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("chirps-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _chirps_like(start, n_time=2, lats=(1.0, 2.0, 3.0), lons=(10.0, 11.0, 12.0, 13.0), fill=1e-6):
    times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D"))
    return xr.Dataset(
        {
            "precipitation_surface": (
                ("time", "latitude", "longitude"),
                np.full((n_time, len(lats), len(lons)), fill, dtype=np.float64),
                {"units": "kg m-2 s-1", "standard_name": "precipitation_flux"},
            )
        },
        coords={
            "time": times.astype("datetime64[ns]"),
            "latitude": list(lats),
            "longitude": list(lons),
        },
    )


def _open_final_only(dataset_id: str):
    if dataset_id == "ucsb-chc-chirps-analysis-final":
        return _chirps_like("2026-01-01", n_time=2)
    return _chirps_like("2026-01-03", n_time=0)


def test_fetch_writes_zarr_with_mocked_catalog(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_catalog", side_effect=_open_final_only):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "precip" in ds
    assert "precipitation_surface" not in ds
    assert ds.sizes["time"] == 2
    assert ds["precip"].attrs.get("units") == "mm day-1"
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    assert "aggregation_period" not in ds["precip"].attrs
    assert ds.attrs.get("weather_skills_source") == "chirps"
    assert load_history(out)[-1]["skill"] == "chirps-fetch"


def test_missing_days_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    def empty(_dataset_id: str):
        return _chirps_like("2026-01-01", n_time=0)

    with patch.object(mod, "_open_catalog", side_effect=empty):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-01",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_probe_latest_uses_catalog_time(capsys, fetch, mod):
    def open_catalog(dataset_id: str):
        if dataset_id.endswith("preliminary"):
            return _chirps_like("2026-09-10", n_time=6)
        return _chirps_like("2026-08-01", n_time=31)

    with patch.object(mod, "_open_catalog", side_effect=open_catalog):
        run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-09-15"


def test_catalog_ids(mod):
    assert mod._FINAL_ID == "ucsb-chc-chirps-analysis-final"
    assert mod._PRELIM_ID == "ucsb-chc-chirps-analysis-preliminary"


def test_fetch_bbox_subsets_space(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_catalog", side_effect=_open_final_only):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-01",
            "--bbox",
            "2.5/10.5/1.5/12.5",
            "-o",
            str(out),
        )

    ds = xr.open_zarr(out, consolidated=True)
    assert float(ds.latitude.min()) >= 1.5
    assert float(ds.latitude.max()) <= 2.5
    assert float(ds.longitude.min()) >= 10.5
    assert float(ds.longitude.max()) <= 12.5
    assert ds.sizes["time"] == 1


def test_empty_bbox_exits(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_catalog", side_effect=_open_final_only):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-01",
                "--bbox",
                "50/10/40/12",
                "-o",
                str(out),
            )
    assert exc.value.code == 1


def test_prelim_fills_tail_after_final(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    def open_catalog(dataset_id: str):
        if dataset_id.endswith("preliminary"):
            return _chirps_like("2026-01-02", n_time=2, fill=2e-6)
        return _chirps_like("2026-01-01", n_time=2, fill=1e-6)

    with patch.object(mod, "_open_catalog", side_effect=open_catalog):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-03",
            "-o",
            str(out),
        )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 3
    days = [str(t)[:10] for t in ds.time.values]
    assert days == ["2026-01-01", "2026-01-02", "2026-01-03"]
    # Overlap day keeps final (smaller fill) rather than prelim.
    day2 = float(ds.precip.isel(time=1, latitude=0, longitude=0))
    day3 = float(ds.precip.isel(time=2, latitude=0, longitude=0))
    assert day3 > day2


def test_interior_hole_exits_2(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    def open_catalog(_dataset_id: str):
        ds = _chirps_like("2026-01-01", n_time=3)
        return ds.isel(time=[0, 2])

    with patch.object(mod, "_open_catalog", side_effect=open_catalog):
        with pytest.raises(SystemExit) as exc:
            run_skill(
                fetch,
                "--start-time",
                "2026-01-01",
                "--end-time",
                "2026-01-03",
                "-o",
                str(out),
            )
    assert exc.value.code == 2


def test_workers_flag_still_accepted(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_catalog", side_effect=_open_final_only):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--workers",
            "8",
            "-o",
            str(out),
        )
    assert Path(out).exists()

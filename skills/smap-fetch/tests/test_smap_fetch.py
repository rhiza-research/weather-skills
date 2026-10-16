"""Correctness tests for smap-fetch (mocked Earthdata; auth failure)."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import DataError


@pytest.fixture(scope="module")
def mod():
    return load_skill("smap-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_login_without_credentials_raises_data_error(mod, tmp_path):
    env = {
        k: v for k, v in os.environ.items() if k not in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD")
    }
    # Keep a real ~/.netrc on the dev machine from satisfying the netrc strategy.
    env["NETRC"] = str(tmp_path / "missing.netrc")
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(DataError, match="Earthdata authentication failed"):
            mod._login()


class _FakeDataset:
    def __init__(self, values, **attrs):
        self.values = np.asarray(values)
        self.attrs = attrs

    def __getitem__(self, key):
        return self.values[key]


class _FakeH5File(dict):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_h5py(group, suffix, units=b"cm**3/cm**3"):
    """A minimal stand-in for h5py exposing one SPL3SMP_E retrieval group."""
    sm = np.array([[0.10, 0.20, -9999.0], [0.30, 0.60, 0.40]], dtype="float32")
    qual = np.array([[0, 1, 65534], [0, 0, 8]], dtype="uint16")
    lat = np.array([[1.0, 1.0, -9999.0], [0.5, 0.5, 0.5]], dtype="float32")
    lon = np.array([[10.0, 11.0, -9999.0], [10.0, 11.0, 12.0]], dtype="float32")
    grp = {
        f"soil_moisture{suffix}": _FakeDataset(
            sm, units=units, valid_min=np.float32(0.02), valid_max=np.float32(0.5)
        ),
        f"retrieval_qual_flag{suffix}": _FakeDataset(qual),
        f"latitude{suffix}": _FakeDataset(lat),
        f"longitude{suffix}": _FakeDataset(lon),
    }
    fake = MagicMock()
    fake.File = lambda *_a, **_k: _FakeH5File({f"Soil_Moisture_Retrieval_Data_{group}": grp})
    return fake


def test_pm_overpass_reads_pm_suffixed_datasets(mod):
    # Real PM granules name their datasets soil_moisture_pm / latitude_pm / ...
    with patch.dict(sys.modules, {"h5py": _fake_h5py("PM", "_pm")}):
        da = mod._slice_from_file("x.h5", "PM", "2024-06-01", True)
    assert da.shape == (2, 3)
    np.testing.assert_allclose(da["latitude"], [1.0, 0.5])
    np.testing.assert_allclose(da["longitude"], [10.0, 11.0, 12.0])


def test_fill_out_of_range_and_quality_masking(mod):
    with patch.dict(sys.modules, {"h5py": _fake_h5py("AM", "")}):
        rec = mod._slice_from_file("x.h5", "AM", "2024-06-01", True).values
        allq = mod._slice_from_file("x.h5", "AM", "2024-06-01", False).values
    # -9999 fill and 0.60 (> valid_max 0.5) are NaN, never 0 or kept.
    np.testing.assert_allclose(allq, [[0.10, 0.20, np.nan], [0.30, np.nan, 0.40]], rtol=1e-6)
    # recommended also drops the bit-0 (not recommended) retrieval at 0.20.
    np.testing.assert_allclose(rec, [[0.10, np.nan, np.nan], [0.30, np.nan, 0.40]], rtol=1e-6)


def test_non_ratio_units_are_rejected(mod):
    with patch.dict(sys.modules, {"h5py": _fake_h5py("AM", "", units=b"kg m-2")}):
        with pytest.raises(RuntimeError, match="dimensionless volumetric ratio"):
            mod._slice_from_file("x.h5", "AM", "2024-06-01", True)


def test_fetch_writes_zarr_with_mocked_earthaccess(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    granules = []
    for day in ("20260101", "20260102"):
        granule = MagicMock()
        granule.data_links.return_value = [f"https://example/SPL3SMP_E_{day}_001.h5"]
        granules.append(granule)

    sm = np.array([[0.2, 0.3], [0.4, 0.5]])
    staged = []

    def fake_download(_granules, local_path):
        path = Path(local_path) / f"g{len(staged)}.h5"
        path.write_bytes(b"")
        staged.append(path)
        return [str(path)]

    def fake_slice_from_file(_path, _group, _day_iso, _recommended_only):
        return xr.DataArray(
            sm,
            dims=("latitude", "longitude"),
            coords={"latitude": [1.0, 2.0], "longitude": [10.0, 11.0]},
            name="soil_moisture",
        )

    with (
        patch.object(mod, "_login"),
        patch("earthaccess.search_data", return_value=granules),
        patch("earthaccess.download", side_effect=fake_download),
        patch.object(mod, "_slice_from_file", side_effect=fake_slice_from_file),
    ):
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
    assert len(staged) == 2
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2
    assert ds["soil_moisture"].attrs.get("data_interval") == "1 day"
    assert ds["soil_moisture"].attrs["units"] == "m3 m-3"
    # The CF grid_mapping variable stays a scalar, not broadcast along time.
    assert ds["latitude_longitude"].dims == ()

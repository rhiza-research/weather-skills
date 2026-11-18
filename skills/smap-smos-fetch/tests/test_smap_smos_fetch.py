"""Correctness tests for smap-smos-fetch (mocked Earthdata and CATDS; auth failure)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import DataError


@pytest.fixture(scope="module")
def mod():
    return load_skill("smap-smos-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_login_without_credentials_raises_data_error(mod, tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD", "EARTHDATA_TOKEN")
    }
    # Keep a real ~/.netrc on the dev machine from satisfying the netrc strategy.
    env["NETRC"] = str(tmp_path / "missing.netrc")
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(DataError, match="Earthdata authentication failed"):
            mod._login()


class _FakeDataset:
    def __init__(self, values, stored=True, **attrs):
        self.values = np.asarray(values)
        self.attrs = attrs
        self.id = MagicMock()
        self.id.get_storage_size.return_value = self.values.nbytes if stored else 0

    def __getitem__(self, key):
        return self.values[key]


_SM = np.array([[0.10, 0.20, -9999.0], [0.30, 0.60, 0.40]], dtype="float32")
_QUAL = np.array([[0, 1, 65534], [0, 0, 8]], dtype="uint16")


def _group(suffix, units=b"cm**3/cm**3", stored=True):
    return {
        f"soil_moisture{suffix}": _FakeDataset(
            _SM, stored, units=units, valid_min=np.float32(0.02), valid_max=np.float32(0.5)
        ),
        f"retrieval_qual_flag{suffix}": _FakeDataset(_QUAL),
    }


def _spl3_file(**kw):
    """A stand-in SPL3SMP_E file: AM datasets unsuffixed, PM ones "_pm"."""
    return {
        "Soil_Moisture_Retrieval_Data_AM": _group("", **kw),
        "Soil_Moisture_Retrieval_Data_PM": _group("_pm", **kw),
    }


def _nsidc0800_file(**kw):
    """A stand-in NSIDC-0800 file: one group per retrieval, "_am"/"_pm" suffixes."""
    return {
        base: {
            "Soil_Moisture_Retrieval_Data_AM": _group("_am", **kw),
            "Soil_Moisture_Retrieval_Data_PM": _group("_pm", **kw),
        }
        for base in ("SMAP_Based", "SMOS_Based")
    }


ALL = (slice(None), slice(None))


@pytest.mark.parametrize("overpass", ["AM", "PM"])
def test_spl3_fill_out_of_range_and_quality_masking(mod, overpass):
    h = _spl3_file()
    rec = mod._read_nasa(h, mod._SPL3, "smap", overpass, *ALL, True, "2024-06-01")
    allq = mod._read_nasa(h, mod._SPL3, "smap", overpass, *ALL, False, "2024-06-01")
    # -9999 fill and 0.60 (> valid_max 0.5) are NaN, never 0 or kept.
    np.testing.assert_allclose(allq, [[0.10, 0.20, np.nan], [0.30, np.nan, 0.40]], rtol=1e-6)
    # recommended also drops the bit-0 (not recommended) retrieval at 0.20.
    np.testing.assert_allclose(rec, [[0.10, np.nan, np.nan], [0.30, np.nan, 0.40]], rtol=1e-6)


def test_nsidc0800_reads_each_retrieval_group(mod):
    h = _nsidc0800_file()
    h["SMOS_Based"]["Soil_Moisture_Retrieval_Data_PM"]["soil_moisture_pm"].values = np.full(
        (2, 3), 0.25, dtype="float32"
    )
    smos_pm = mod._read_nasa(h, mod._NSIDC0800, "smos", "PM", *ALL, False, "2024-06-01")
    np.testing.assert_allclose(smos_pm, np.full((2, 3), 0.25))
    smap_am = mod._read_nasa(h, mod._NSIDC0800, "smap", "AM", 0, slice(0, 2), False, "x")
    np.testing.assert_allclose(smap_am, [0.10, 0.20], rtol=1e-6)


def test_empty_placeholder_field_returns_none(mod):
    # A granule with nothing stored reads as zeros in h5py; it must not become data.
    h = _nsidc0800_file(stored=False)
    assert mod._read_nasa(h, mod._NSIDC0800, "smap", "AM", *ALL, True, "2026-10-05") is None


def test_non_ratio_units_are_rejected(mod):
    h = _spl3_file(units=b"kg m-2")
    with pytest.raises(RuntimeError, match="dimensionless volumetric ratio"):
        mod._read_nasa(h, mod._SPL3, "smap", "AM", *ALL, True, "2024-06-01")


def test_missing_group_names_product_and_overpass(mod):
    with pytest.raises(RuntimeError, match="no smap PM soil moisture"):
        mod._read_nasa({}, mod._SPL3, "smap", "PM", *ALL, True, "2024-06-01")


def test_ease9_grid_matches_the_product_geolocation():
    pytest.importorskip("pyproj")
    lat, lon = load_skill("smap-smos-fetch", "fetch")._ease9_grid()
    assert lat.shape == (1624,) and lon.shape == (3856,)
    # Values read from an SPL3SMP_E file's latitude/longitude over Kenya.
    np.testing.assert_allclose(lat[(lat > 5.47) & (lat < 5.49)], [5.4804697], atol=1e-5)
    assert np.all(np.diff(lat) < 0) and np.all(np.diff(lon) > 0)


def test_box_index_selects_cells_and_crosses_the_antimeridian(mod):
    lat = np.array([3.0, 2.0, 1.0, 0.0])
    lon = np.array([-179.5, -10.0, 0.0, 10.0, 179.5])
    rows, cols = mod._box_index(lat, lon, (2.5, -5.0, 0.5, 15.0))
    np.testing.assert_array_equal(lat[rows], [2.0, 1.0])
    np.testing.assert_array_equal(lon[cols], [0.0, 10.0])
    _, cols = mod._box_index(lat, lon, (2.5, 170.0, 0.5, -170.0))
    np.testing.assert_array_equal(lon[cols], [-179.5, 179.5])


# --- whole runs on a tiny 2 x 3 grid -------------------------------------------------

_LAT = np.array([1.0, 0.0])  # north to south, like the EASE grid
_LON = np.array([10.0, 11.0, 12.0])
NAN = np.nan


def _granules(*days):
    out = []
    for day in days:
        g = MagicMock()
        g.data_links.return_value = [f"https://example/SMAP_L3_SM_P_E_{day}_R1_001.h5"]
        out.append(g)
    return out


def _run(mod, fetch, tmp_path, nasa_fields, catds_fields=None, *args):
    """Run fetch with every remote read faked.

    ``nasa_fields[day_iso]`` is the list _read_nasa_day returns for that day;
    ``catds_fields[(day_iso, overpass)]`` the CATDS DataArray (None: no file).
    """
    out = tmp_path / "out.zarr"
    days = sorted(nasa_fields)

    def fake_read_nasa_day(_g, _product, layers, _rows, _cols, _rec, day_iso):
        fields = nasa_fields[day_iso]
        assert len(fields) == len(layers)
        return fields

    def fake_catds_day(_session, day, overpass, _bbox, _workdir):
        return (catds_fields or {}).get((day.isoformat(), overpass))

    login = MagicMock()
    with (
        patch.object(mod, "_ease9_grid", return_value=(_LAT, _LON)),
        patch.object(mod, "_login", login),
        patch(
            "earthaccess.search_data", return_value=_granules(*(d.replace("-", "") for d in days))
        ),
        patch.object(mod, "_read_nasa_day", side_effect=fake_read_nasa_day),
        patch.object(mod, "_catds_day", side_effect=fake_catds_day),
    ):
        run_skill(fetch, *args, "-o", str(out))
    return xr.open_zarr(out, consolidated=True), login


def test_native_smap_writes_cf_zarr_and_keeps_empty_days(mod, fetch, tmp_path, capsys):
    a = np.array([[0.2, 0.3, NAN], [0.4, NAN, 0.1]], dtype="float32")
    ds, _ = _run(
        mod, fetch, tmp_path,
        {"2026-01-01": [a], "2026-01-02": [None], "2026-01-03": [a]},
        None,
        "--start-time", "2026-01-01", "--end-time", "2026-01-04",
    )  # fmt: skip
    # 01-02's field is empty and 01-04 has no granule: both kept, NaN.
    assert ds.sizes == {"time": 4, "latitude": 2, "longitude": 3}
    has_data = ds["soil_moisture"].notnull().any(("latitude", "longitude")).values
    np.testing.assert_array_equal(has_data, [True, False, True, False])
    # Latitude comes out ascending.
    np.testing.assert_allclose(ds["latitude"], [0.0, 1.0])
    np.testing.assert_allclose(ds["soil_moisture"].isel(time=0), a[::-1], rtol=1e-6)
    assert ds["soil_moisture"].attrs["units"] == "m3 m-3"
    assert ds["soil_moisture"].attrs.get("data_interval") == "1 day"
    # The CF grid_mapping variable stays a scalar, not broadcast along time.
    assert ds["latitude_longitude"].dims == ()
    assert "soil_moisture_source" not in ds
    assert ds.attrs["weather_skills_source"] == "nsidc:SPL3SMP_E"
    err = capsys.readouterr().err
    assert "no SPL3SMP_E file for 1 day(s)" in err and "2026-01-04" in err


def test_both_sensors_both_overpasses_fill_in_order(mod, fetch, tmp_path):
    smap_am = np.array([[0.1, NAN, NAN], [NAN, NAN, NAN]], dtype="float32")
    smap_pm = np.array([[0.9, 0.3, NAN], [NAN, NAN, NAN]], dtype="float32")
    # CATDS on its own coarser grid; each 9 km cell takes the nearest SMOS cell.
    smos = xr.DataArray(
        np.array([[0.5, 0.6, 0.65], [0.7, 0.8, 0.85]], dtype="float32"),
        dims=("latitude", "longitude"),
        coords={"latitude": [0.05, 0.95], "longitude": [10.1, 11.1, 11.9]},
    )
    ds, _ = _run(
        mod, fetch, tmp_path,
        {"2026-01-01": [smap_am, smap_pm]},
        {("2026-01-01", "AM"): smos, ("2026-01-01", "PM"): None},
        "--start-time", "2026-01-01", "--end-time", "2026-01-01",
        "--sensor", "both", "--overpass", "both",
    )  # fmt: skip
    sm = ds["soil_moisture"].isel(time=0).values  # rows: lat 0.0, 1.0
    src = ds["soil_moisture_source"].isel(time=0).values
    # Row lat 1.0: SMAP AM at lon 10, then the nearest SMOS AM cells.
    np.testing.assert_allclose(sm[1], [0.1, 0.8, 0.85], rtol=1e-6)
    np.testing.assert_array_equal(src[1], [1, 2, 2])
    # Row lat 0.0: SMOS AM everywhere; SMAP PM never reached.
    np.testing.assert_allclose(sm[0], [0.5, 0.6, 0.65], rtol=1e-6)
    assert (
        ds["soil_moisture_source"].attrs["flag_meanings"] == "none smap_am smos_am smap_pm smos_pm"
    )
    assert ds.attrs["weather_skills_source"] == "nsidc:SPL3SMP_E+catds:smos-l3sm"
    assert "acknowledgement" in ds.attrs


def test_intercalibrated_reads_all_four_layers_from_one_granule(mod, fetch, tmp_path):
    f = lambda v: np.full((2, 3), v, dtype="float32")  # noqa: E731
    holes = f(0.2)
    holes[0, 0] = NAN
    ds, _ = _run(
        mod, fetch, tmp_path,
        {"2026-01-01": [holes, f(0.3), f(0.4), f(0.5)]},
        None,
        "--start-time", "2026-01-01", "--end-time", "2026-01-01",
        "--sensor", "both", "--product", "intercalibrated", "--overpass", "both",
    )  # fmt: skip
    src = ds["soil_moisture_source"].isel(time=0).values
    # The SMAP AM hole (lat 1.0, lon 10) is filled from SMOS AM.
    np.testing.assert_array_equal(src, [[1, 1, 1], [2, 1, 1]])
    assert ds.attrs["weather_skills_source"] == "nsidc:NSIDC-0800:both"


def test_native_smos_needs_no_login_and_keeps_its_grid(mod, fetch, tmp_path):
    smos = xr.DataArray(
        np.array([[0.5, 0.6], [0.7, NAN]], dtype="float32"),
        dims=("latitude", "longitude"),
        coords={"latitude": [-0.1, 0.2], "longitude": [30.0, 30.3]},
    )
    ds, login = _run(
        mod, fetch, tmp_path,
        {},
        {("2026-01-01", "AM"): smos},
        "--start-time", "2026-01-01", "--end-time", "2026-01-02", "--sensor", "smos",
    )  # fmt: skip
    login.assert_not_called()
    np.testing.assert_allclose(ds["latitude"], [-0.1, 0.2])
    np.testing.assert_allclose(ds["longitude"], [30.0, 30.3])
    np.testing.assert_allclose(ds["soil_moisture"].isel(time=0), smos, rtol=1e-6)
    assert ds["soil_moisture"].isel(time=1).isnull().all()
    assert ds.attrs["weather_skills_source"] == "catds:smos-l3sm"


def test_no_usable_data_anywhere_is_a_data_error(mod, fetch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run(
            mod, fetch, tmp_path,
            {"2026-01-01": [None]},
            None,
            "--start-time", "2026-01-01", "--end-time", "2026-01-01",
        )  # fmt: skip
    assert exc.value.code == 1
    assert not Path(tmp_path / "out.zarr").exists()

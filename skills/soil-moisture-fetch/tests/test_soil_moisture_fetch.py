"""Correctness tests for soil-moisture-fetch (mocked CDS, Earthdata and CATDS; auth failure)."""

import os
import sys
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import DataError, UsageError


@pytest.fixture(scope="module")
def mod():
    return load_skill("soil-moisture-fetch", "fetch")


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


def _read(mod, h, dataset, sensor, overpass, rows, cols, rec, day_iso):
    """One layer from a stand-in granule ``h`` through _read_nasa (HTTPS open and h5py faked)."""
    handle = MagicMock(__enter__=lambda s: h, __exit__=lambda *a: None)
    with (
        patch("earthaccess.open", return_value=["handle"]),
        patch.dict(sys.modules, {"h5py": MagicMock(File=MagicMock(return_value=handle))}),
    ):
        (field,) = mod._read_nasa(None, dataset, [(sensor, overpass)], rows, cols, rec, day_iso)
    return field


@pytest.mark.parametrize("overpass", ["AM", "PM"])
def test_spl3_fill_out_of_range_and_quality_masking(mod, overpass):
    h = _spl3_file()
    rec = _read(mod, h, "smap", "smap", overpass, *ALL, True, "2024-06-01")
    allq = _read(mod, h, "smap", "smap", overpass, *ALL, False, "2024-06-01")
    # -9999 fill and 0.60 (> valid_max 0.5) are NaN, never 0 or kept.
    np.testing.assert_allclose(allq, [[0.10, 0.20, np.nan], [0.30, np.nan, 0.40]], rtol=1e-6)
    # recommended also drops the bit-0 (not recommended) retrieval at 0.20.
    np.testing.assert_allclose(rec, [[0.10, np.nan, np.nan], [0.30, np.nan, 0.40]], rtol=1e-6)


def test_nsidc0800_reads_each_retrieval_group(mod):
    h = _nsidc0800_file()
    h["SMOS_Based"]["Soil_Moisture_Retrieval_Data_PM"]["soil_moisture_pm"].values = np.full(
        (2, 3), 0.25, dtype="float32"
    )
    smos_pm = _read(mod, h, "smap-smos", "smos", "PM", *ALL, False, "2024-06-01")
    np.testing.assert_allclose(smos_pm, np.full((2, 3), 0.25))
    smap_am = _read(mod, h, "smap-smos", "smap", "AM", 0, slice(0, 2), False, "x")
    np.testing.assert_allclose(smap_am, [0.10, 0.20], rtol=1e-6)


def test_empty_placeholder_field_returns_none(mod):
    # A granule with nothing stored reads as zeros in h5py; it must not become data.
    h = _nsidc0800_file(stored=False)
    assert _read(mod, h, "smap-smos", "smap", "AM", *ALL, True, "2026-10-05") is None


def test_non_ratio_units_are_rejected(mod):
    h = _spl3_file(units=b"kg m-2")
    with pytest.raises(DataError, match="dimensionless volumetric ratio"):
        _read(mod, h, "smap", "smap", "AM", *ALL, True, "2024-06-01")


def test_missing_group_names_product_and_overpass(mod):
    with pytest.raises(DataError, match="no smap PM soil moisture"):
        _read(mod, {}, "smap", "smap", "PM", *ALL, True, "2024-06-01")


def test_ease9_grid_matches_the_product_geolocation(mod):
    pytest.importorskip("pyproj")
    lat, lon, _, _ = mod._ease9_box(None)
    assert lat.shape == (1624,) and lon.shape == (3856,)
    # Values read from an SPL3SMP_E file's latitude/longitude over Kenya.
    np.testing.assert_allclose(lat[(lat > 5.47) & (lat < 5.49)], [5.4804697], atol=1e-5)
    assert np.all(np.diff(lat) < 0) and np.all(np.diff(lon) > 0)


def test_ease9_box_selects_cells_and_crosses_the_antimeridian(mod):
    pytest.importorskip("pyproj")
    lat, lon, _, _ = mod._ease9_box((2.5, -5.0, 0.5, 15.0))
    assert lat.min() >= 0.5 and lat.max() <= 2.5 and lon.min() >= -5 and lon.max() <= 15
    _, lon, _, cols = mod._ease9_box((2.5, 179.0, 0.5, -179.0))
    assert set(np.sign(lon)) == {-1.0, 1.0} and np.all(np.abs(lon) >= 179.0)


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

    ``nasa_fields[day_iso]`` is the list _read_nasa returns for that day;
    ``catds_fields[(day_iso, overpass)]`` the CATDS DataArray (None: no file).
    """
    out = tmp_path / "out.zarr"
    days = sorted(nasa_fields)

    def fake_read_nasa(_g, _dataset, layers, _rows, _cols, _rec, day_iso):
        fields = nasa_fields[day_iso]
        assert len(fields) == len(layers)
        return fields

    def fake_catds_day(_session, day, overpass, _bbox, _workdir):
        return (catds_fields or {}).get((day.isoformat(), overpass))

    login = MagicMock()
    with (
        patch.object(mod, "_ease9_box", return_value=(_LAT, _LON, slice(None), slice(None))),
        patch.object(mod, "_login", login),
        patch(
            "earthaccess.search_data", return_value=_granules(*(d.replace("-", "") for d in days))
        ),
        patch.object(mod, "_read_nasa", side_effect=fake_read_nasa),
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
        "--dataset", "smap", "--start-time", "2026-01-01", "--end-time", "2026-01-04",
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
    assert "soil_moisture_source" not in ds
    assert ds.attrs["weather_skills_source"] == "nsidc:SPL3SMP_E"
    err = capsys.readouterr().err
    assert "no SPL3SMP_E file (left empty) for 1 day(s)" in err and "2026-01-04" in err


def test_intercalibrated_reads_all_four_layers_from_one_granule(mod, fetch, tmp_path):
    f = lambda v: np.full((2, 3), v, dtype="float32")  # noqa: E731
    holes = f(0.2)
    holes[0, 0] = NAN
    ds, _ = _run(
        mod, fetch, tmp_path,
        {"2026-01-01": [holes, f(0.3), f(0.4), f(0.5)]},
        None,
        "--start-time", "2026-01-01", "--end-time", "2026-01-01",
        "--dataset", "smap-smos", "--overpass", "both",
    )  # fmt: skip
    src = ds["soil_moisture_source"].isel(time=0).values
    # The SMAP AM hole (lat 1.0, lon 10) is filled from SMOS AM.
    np.testing.assert_array_equal(src, [[1, 1, 1], [2, 1, 1]])
    assert ds.attrs["weather_skills_source"] == "nsidc:NSIDC-0800"
    assert (
        ds["soil_moisture_source"].attrs["flag_meanings"] == "none smap_am smos_am smap_pm smos_pm"
    )


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
        "--dataset", "smos", "--start-time", "2026-01-01", "--end-time", "2026-01-02",
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
            "--dataset", "smap", "--start-time", "2026-01-01", "--end-time", "2026-01-01",
        )  # fmt: skip
    assert exc.value.code == 1
    assert not Path(tmp_path / "out.zarr").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--dataset", "ascat", "--overpass", "AM"],
        ["--dataset", "smos", "--quality", "all"],
        ["--dataset", "smap", "--uncertainty"],
        ["--dataset", "smap", "--start-time", "2015-01-01", "--end-time", "2015-01-02"],
    ],
)
def test_options_that_do_not_apply_are_usage_errors(fetch, tmp_path, args):
    if "--start-time" not in args:
        args = [*args, "--start-time", "2026-01-01", "--end-time", "2026-01-02"]
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, *args, "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_a_failed_granule_read_is_retried(mod):
    h = _spl3_file()
    opened = MagicMock(side_effect=[OSError("connection reset"), ["handle"]])
    handle = MagicMock(__enter__=lambda s: h, __exit__=lambda *a: None)
    with (
        patch("earthaccess.open", opened),
        patch.dict(sys.modules, {"h5py": MagicMock(File=MagicMock(return_value=handle))}),
        patch.object(mod.time, "sleep"),
    ):
        (field,) = mod._read_nasa(None, "smap", [("smap", "AM")], *ALL, False, "2024-06-01")
    assert opened.call_count == 2 and field.shape == (2, 3)


# --- CDS (ascat, combined) ---------------------------------------------------------


def _constraints(sensor, variable):
    return [
        # Monthly rows and other sensors are ignored.
        {"type_of_sensor": [sensor], "time_aggregation": ["monthly"], "variable": [variable],
         "version": ["v202505"], "type_of_record": ["cdr"], "year": ["2024"], "month": ["01"], "day": ["01"]},
        {"type_of_sensor": ["passive"], "time_aggregation": ["daily"], "variable": [variable],
         "version": ["v202505"], "type_of_record": ["cdr"], "year": ["2024"], "month": ["01"], "day": ["01"]},
        {"type_of_sensor": [sensor], "time_aggregation": ["daily"], "variable": [variable],
         "version": ["v202312", "v202505", "deprecated_v202212"], "type_of_record": ["cdr"],
         "year": ["2024"], "month": ["02"], "day": ["28", "29", "30"]},
        {"type_of_sensor": [sensor], "time_aggregation": ["daily"], "variable": [variable],
         "version": ["v202505_2"], "type_of_record": ["icdr"], "year": ["2024"], "month": ["02"], "day": ["29"]},
    ]  # fmt: skip


@pytest.mark.parametrize("dataset", ["ascat", "combined"])
def test_cds_days_keeps_the_newest_daily_version(mod, dataset):
    sensor, variable, *_ = mod._CDS_VARS[dataset]
    responses = [
        MagicMock(json=lambda: {"links": [{"rel": "constraints", "href": "https://x/c"}]}),
        MagicMock(json=lambda: _constraints(sensor, variable)),
    ]
    with patch("requests.get", side_effect=responses):
        days = mod._cds_days(dataset)
    # 30 February is dropped; 29 February takes v202505_2 (icdr) over v202505.
    assert days == {
        date(2024, 2, 28): ("v202505", "cdr"),
        date(2024, 2, 29): ("v202505_2", "icdr"),
    }


def _cds_key_used(mod):
    """The key _fetch_cds hands the CDS client (the client stops the run right there)."""
    seen = {}

    class Stop(Exception):
        pass

    def client(url, key):
        seen["key"] = key
        raise Stop

    with (
        patch.object(mod, "_cds_days", return_value={date(2024, 1, 1): ("v202505", "cdr")}),
        patch.dict(sys.modules, {"ecmwf.datastores": MagicMock(Client=client),
                                 "ecmwf.datastores.processing": MagicMock()}),
        pytest.raises(Stop),
    ):  # fmt: skip
        mod._fetch_cds("ascat", [date(2024, 1, 1)], None, False)
    return seen["key"]


def test_cds_key_comes_from_env_or_a_cds_cdsapirc(mod, tmp_path):
    rc = tmp_path / ".cdsapirc"
    with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
        with pytest.raises(UsageError, match="no CDS credentials"):
            _cds_key_used(mod)
        # An ADS ~/.cdsapirc is not used for the CDS.
        rc.write_text("url: https://ads.atmosphere.copernicus.eu/api\nkey: ads-token\n")
        with pytest.raises(UsageError, match="no CDS credentials"):
            _cds_key_used(mod)
        rc.write_text("url: https://cds.climate.copernicus.eu/api\nkey: cds-token\n")
        assert _cds_key_used(mod) == "cds-token"
    with patch.dict(os.environ, {"HOME": str(tmp_path), "CDS_API_KEY": "env-token"}, clear=True):
        assert _cds_key_used(mod) == "env-token"


def _cds_day_file(path, day, value):
    """A stand-in C3S daily netCDF: global 0.25 deg cells, latitude north to south, like the CDS."""
    lat = np.arange(89.875, -90, -0.25)
    lon = np.arange(-179.875, 180, 0.25)
    xr.Dataset(
        {
            "sm": (("time", "lat", "lon"), np.full((1, lat.size, lon.size), value, "float32")),
            "sm_uncertainty": (
                ("time", "lat", "lon"),
                np.full((1, lat.size, lon.size), 1.0, "float32"),
            ),
        },
        coords={"time": [np.datetime64(day, "ns")], "lat": lat, "lon": lon},
    ).to_netcdf(path)


@pytest.mark.parametrize(
    ("dataset", "var", "units", "tag"),
    [
        ("ascat", "soil_moisture_saturation", "percent", "cds:satellite-soil-moisture:active"),
        ("combined", "soil_moisture", "m3 m-3", "cds:satellite-soil-moisture:combined"),
    ],
)
def test_cds_fetch_cuts_the_box_and_keeps_every_day(mod, fetch, tmp_path, dataset, var, units, tag):
    sensor, variable, *_ = mod._CDS_VARS[dataset]
    requests_made = []

    def submit(_name, request):
        requests_made.append(request)
        remote = MagicMock(results_ready=True)

        def download(target):
            with zipfile.ZipFile(target, "w") as zf:
                for d in request["day"]:
                    nc = tmp_path / f"{d}.nc"
                    _cds_day_file(nc, f"2024-02-{d}", 40.0 + int(d))
                    zf.write(nc, nc.name)

        remote.download.side_effect = download
        return remote

    client = MagicMock()
    client.submit.side_effect = submit
    out = tmp_path / "out.zarr"
    available = {date(2024, 2, 28): ("v202505", "cdr"), date(2024, 2, 29): ("v202505", "cdr")}
    with (
        patch.object(mod, "_cds_days", return_value=available),
        patch.dict(os.environ, {"CDS_API_KEY": "token"}),
        patch.dict(
            sys.modules,
            {
                "ecmwf.datastores": MagicMock(Client=MagicMock(return_value=client)),
                "ecmwf.datastores.processing": MagicMock(
                    ProcessingFailedError=type("PFE", (Exception,), {})
                ),
            },
        ),  # fmt: skip
    ):
        run_skill(fetch, "--dataset", dataset, "--start-time", "2024-02-27", "--end-time", "2024-02-29",
                  "--bbox", "1/34/-1/36", "--uncertainty", "-o", str(out))  # fmt: skip
    ds = xr.open_zarr(out)
    # One request for the month, of the right sensor and variable.
    (req,) = requests_made
    assert (
        req["type_of_sensor"] == [sensor]
        and req["variable"] == [variable]
        and req["day"] == ["28", "29"]
    )
    # 27 February is not on the CDS: kept, NaN.
    assert ds.sizes == {"time": 3, "latitude": 8, "longitude": 8}
    assert ds[var].isel(time=0).isnull().all()
    np.testing.assert_allclose(
        ds[var].isel(time=[1, 2]).mean(("latitude", "longitude")), [68.0, 69.0]
    )
    assert ds[var].attrs["units"] == units and ds[f"{var}_uncertainty"].attrs["units"] == units
    assert np.all(np.diff(ds["latitude"]) > 0)
    assert ds.attrs["weather_skills_source"] == tag
    assert ds[var].attrs.get("data_interval") == "1 day"


# --- review fixes --------------------------------------------------------------------


def test_note_with_no_days_prints_nothing(mod, capsys):
    mod._note("no file (left empty)", [])
    mod._note("no file (left empty)", [date(2026, 1, 1)])
    err = capsys.readouterr().err
    assert err.count("no file") == 1 and "2026-01-01" in err


def test_catds_values_outside_the_valid_range_are_nan(mod, tmp_path):
    import io
    import tarfile

    nc = tmp_path / "SM.nc"
    xr.Dataset(
        {"Soil_Moisture": (("lat", "lon"), np.array([[0.0, 0.3], [0.93, 0.5]], "float32"))},
        coords={"lat": [1.0, 0.0], "lon": [30.0, 30.3]},
    ).to_netcdf(nc)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(nc, arcname="SM.nc")
    listing = MagicMock(status_code=200, text='<a href="SM_X.tgz">')
    archive = MagicMock(status_code=200, content=buf.getvalue())
    session = MagicMock(get=MagicMock(side_effect=[listing, archive]))
    work = tmp_path / "work"
    work.mkdir()
    da = mod._catds_day(session, date(2025, 5, 3), "AM", None, work)
    # 0.0 and 0.93 are outside 0.02-0.5; latitude comes out ascending.
    np.testing.assert_allclose(da.values, [[np.nan, 0.5], [np.nan, 0.3]], rtol=1e-6)


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_nasa_granules_keeps_the_newest_file_per_day(mod, order):
    names = ["SMAP_L3_SM_P_E_20250503_R18290_001.h5", "SMAP_L3_SM_P_E_20250503_R19240_001.h5"]
    found = []
    for i in order:
        g = MagicMock()
        g.data_links.return_value = [f"https://example/{names[i]}"]
        found.append(g)
    with patch("earthaccess.search_data", return_value=found):
        granules = mod._nasa_granules("smap", date(2025, 5, 3), date(2025, 5, 3))
    (g,) = granules.values()
    assert g.data_links()[0].endswith("R19240_001.h5")


@pytest.mark.parametrize("where", ["search", "read"])
def test_earthdata_401_gives_the_login_hint(mod, fetch, tmp_path, capsys, where):
    denied = OSError("401 Unauthorized")
    denied.response = MagicMock(status_code=401)
    search = (
        MagicMock(side_effect=denied)
        if where == "search"
        else MagicMock(return_value=_granules("20260101"))
    )
    with (
        patch.object(mod, "_ease9_box", return_value=(_LAT, _LON, slice(None), slice(None))),
        patch.object(mod, "_login"),
        patch("earthaccess.search_data", search),
        patch("earthaccess.open", MagicMock(side_effect=denied)),
        patch.object(mod.time, "sleep"),
        patch.dict(sys.modules, {"h5py": MagicMock()}),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(fetch, "--dataset", "smap", "--start-time", "2026-01-01", "--end-time", "2026-01-01",
                  "-o", str(tmp_path / "out.zarr"))  # fmt: skip
    assert exc.value.code == 1
    assert "Earthdata authentication failed" in capsys.readouterr().err

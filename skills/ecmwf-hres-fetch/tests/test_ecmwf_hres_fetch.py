"""Correctness tests for ecmwf-hres-fetch (mocked Open Data client)."""

import datetime as dt
import sys
import types

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("ecmwf-hres-fetch", "fetch")


def test_probe_latest_prints_date_usable_as_date_flag(mod, monkeypatch, capsys):
    class _Client:
        def __init__(self, source):
            pass

        def latest(self, **kwargs):
            return dt.datetime(2026, 10, 13, 12)

    opendata = types.ModuleType("ecmwf.opendata")
    opendata.Client = _Client
    monkeypatch.setitem(sys.modules, "ecmwf", types.ModuleType("ecmwf"))
    monkeypatch.setitem(sys.modules, "ecmwf.opendata", opendata)

    run_skill(mod.fetch, "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-13"
    assert "--date 2026-10-13 --run 12" in captured.err


def _fake_opendata(monkeypatch, retrieve):
    class _Client:
        def __init__(self, source):
            self.source = source

        def retrieve(self, request, target):
            retrieve(self.source, request, target)

    opendata = types.ModuleType("ecmwf.opendata")
    opendata.Client = _Client
    monkeypatch.setitem(sys.modules, "ecmwf", types.ModuleType("ecmwf"))
    monkeypatch.setitem(sys.modules, "ecmwf.opendata", opendata)


def _grib_like_t2m(values=None):
    """A dataset shaped like cfgrib's decode of global 2 m temperature (K)."""
    import numpy as np
    import xarray as xr

    steps = np.array([0, 3, 6], dtype="timedelta64[h]").astype("timedelta64[ns]")
    lat = np.arange(90.0, -90.25, -0.25)
    lon = np.arange(-180.0, 180.0, 0.25)
    if values is None:
        values = np.full((steps.size, lat.size, lon.size), 300.0, dtype="float32")
    return xr.Dataset(
        {"t2m": (("step", "latitude", "longitude"), values, {"units": "K"})},
        coords={
            "time": np.datetime64("2026-02-15T00:00", "ns"),
            "step": steps,
            "latitude": lat,
            "longitude": lon,
        },
    )


def test_old_init_falls_back_to_aws_mirror(mod, monkeypatch, tmp_path):
    # data.ecmwf.int keeps only ~4 days of inits; older ones 404 there and
    # used to fail outright although the AWS mirror holds them.
    sources = []

    def retrieve(source, request, target):
        sources.append(source)
        if source == "ecmwf":
            raise RuntimeError("404 Client Error: Not Found for url: https://data.ecmwf.int/...")

    _fake_opendata(monkeypatch, retrieve)
    monkeypatch.setattr(mod, "_open_grib", lambda path: _grib_like_t2m())
    out = tmp_path / "hres.zarr"
    run_skill(
        mod.fetch, "--date", "2026-02-15", "--bbox", "5/34/-5/42", "-v", "t2m", "-o", str(out)
    )

    import xarray as xr

    ds = xr.open_zarr(out)
    assert sources == ["ecmwf", "aws"]
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
    assert float(ds["t2m"].max()) == pytest.approx(26.85)


def test_unpublished_init_reports_both_sources(mod, monkeypatch, tmp_path):
    from weather_skills_core import DataError

    def retrieve(source, request, target):
        raise RuntimeError(f"404 from {source}")

    _fake_opendata(monkeypatch, retrieve)
    with pytest.raises((DataError, SystemExit)):
        run_skill(
            mod.fetch,
            "--date",
            "2026-10-15",
            "--run",
            "18",
            "--bbox",
            "5/34/-5/42",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert not (tmp_path / "x.zarr").exists()


def test_grib_is_read_one_step_at_a_time(mod, monkeypatch, tmp_path):
    # cfgrib allocates the full global grid for every step/level a read asks
    # for before slicing to the bbox, so a whole-cube read of a pressure-level
    # field needed several GB. Each read must cover a single step.
    import numpy as np
    import xarray as xr
    from xarray.backends import BackendArray
    from xarray.core import indexing

    source = _grib_like_t2m()["t2m"].values
    read_steps = []

    class _Recording(BackendArray):
        shape = source.shape
        dtype = source.dtype

        def __getitem__(self, key):
            return indexing.explicit_indexing_adapter(
                key, self.shape, indexing.IndexingSupport.BASIC, self._raw
            )

        def _raw(self, key):
            read_steps.append(len(np.arange(self.shape[0])[key[0]]))
            return source[key]

    lazy = xr.Variable(
        ("step", "latitude", "longitude"),
        indexing.LazilyIndexedArray(_Recording()),
        {"units": "K"},
    )
    grib = _grib_like_t2m().assign(t2m=lazy)

    _fake_opendata(monkeypatch, lambda source, request, target: None)
    monkeypatch.setattr(mod, "_open_grib", lambda path: grib)
    run_skill(
        mod.fetch,
        "--date",
        "2026-10-15",
        "--bbox",
        "5/34/-5/42",
        "-v",
        "t2m",
        "-o",
        str(tmp_path / "hres.zarr"),
    )
    assert read_steps and max(read_steps) == 1


def test_variables_match_open_data_catalog(mod):
    # Open Data has no `cape` (only `mucape`), and serves 600 hPa.
    assert "cape" not in mod.VARIABLES
    req = mod._build_request("2026-10-15", 0, ["mucape"], "sfc", ())
    assert req["param"] == ["mucape"]
    req = mod._build_request("2026-10-15", 0, ["t"], "pl", mod.PRESSURE_LEVELS)
    assert "600" in req["levelist"]
    assert {"1000", "50"} <= set(req["levelist"])

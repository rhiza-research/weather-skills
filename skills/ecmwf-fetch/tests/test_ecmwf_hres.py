"""Correctness tests for ecmwf-fetch --product hres (mocked Open Data client)."""

import datetime as dt
import sys
import types

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("ecmwf-fetch", "fetch")


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

    run_skill(mod.fetch, "--product", "hres", "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-13"
    assert "--date 2026-10-13 --run 12" in captured.err


def _fake_opendata(monkeypatch, retrieve, clients=None):
    class _Client:
        def __init__(self, source, **kwargs):
            self.source = source
            if clients is not None:
                clients.append({"source": source, **kwargs})

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


def test_old_init_goes_straight_to_aws_mirror(mod, monkeypatch, tmp_path):
    # data.ecmwf.int keeps only 4 days of inits; asking it for an older one
    # always 404s, so only the AWS mirror is tried.
    sources = []
    _fake_opendata(monkeypatch, lambda source, request, target: sources.append(source))
    monkeypatch.setattr(mod, "_open_grib", lambda path: _grib_like_t2m())
    out = tmp_path / "hres.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "hres",
        "--date",
        "2026-02-15",
        "--bbox",
        "5/34/-5/42",
        "-v",
        "t2m",
        "-o",
        str(out),
    )

    import xarray as xr

    ds = xr.open_zarr(out)
    assert sources == ["aws"]
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
    assert float(ds["t2m"].max()) == pytest.approx(26.85)


@pytest.mark.parametrize("days_ago, sources_tried", [(3, ["ecmwf", "aws"]), (4, ["aws"])])
def test_ecmwf_is_tried_for_the_last_4_days_of_inits(
    mod, monkeypatch, tmp_path, days_ago, sources_tried
):
    sources = []

    def retrieve(source, request, target):
        sources.append(source)
        if source == "ecmwf":
            raise RuntimeError("404 Client Error: Not Found for url: https://data.ecmwf.int/...")

    _fake_opendata(monkeypatch, retrieve)
    monkeypatch.setattr(mod, "_open_grib", lambda path: _grib_like_t2m())
    date = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days_ago)
    run_skill(
        mod.fetch,
        "--product",
        "hres",
        "--date",
        date.isoformat(),
        "--bbox",
        "5/34/-5/42",
        "-v",
        "t2m",
        "-o",
        str(tmp_path / "hres.zarr"),
    )
    assert sources == sources_tried


def test_max_lead_limits_requested_steps(mod, monkeypatch, tmp_path):
    requests = []
    _fake_opendata(monkeypatch, lambda source, request, target: requests.append(request))
    monkeypatch.setattr(mod, "_open_grib", lambda path: _grib_like_t2m())
    run_skill(
        mod.fetch,
        "--product",
        "hres",
        "--date",
        "2026-02-15",
        "--bbox",
        "5/34/-5/42",
        "-v",
        "t2m",
        "--max-lead",
        "6h",
        "-o",
        str(tmp_path / "hres.zarr"),
    )
    assert requests[0]["step"] == [0, 3, 6]


def test_max_lead_0_keeps_a_step_dim(mod, monkeypatch, tmp_path):
    # cfgrib decodes a one-step GRIB with step as a scalar coordinate.
    _fake_opendata(monkeypatch, lambda source, request, target: None)
    monkeypatch.setattr(mod, "_open_grib", lambda path: _grib_like_t2m().isel(step=0))
    out = tmp_path / "hres.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "hres",
        "--date",
        "2026-02-15",
        "--bbox",
        "5/34/-5/42",
        "-v",
        "t2m",
        "--max-lead",
        "0",
        "-o",
        str(out),
    )
    import xarray as xr

    assert xr.open_zarr(out).sizes["step"] == 1


def test_max_lead_fetches_one_more_step_for_precip(mod, monkeypatch, tmp_path):
    # precip turns into rates over [step, next step) and drops its last step, so
    # --max-lead 0 must still return the 0-3 h rate.
    requests = []
    _fake_opendata(monkeypatch, lambda source, request, target: requests.append(request))

    def tp_grib(path):
        ds = _grib_like_t2m().isel(step=[0, 1]).rename({"t2m": "tp"})
        ds["tp"] = ds["tp"].copy(data=ds["tp"].values * [[[0.0]], [[0.003]]])
        ds["tp"].attrs = {"units": "m"}
        return ds

    monkeypatch.setattr(mod, "_open_grib", tp_grib)
    out = tmp_path / "hres.zarr"
    run_skill(
        mod.fetch,
        "--product",
        "hres",
        "--date",
        "2026-02-15",
        "--bbox",
        "5/34/-5/42",
        "-v",
        "tp",
        "--max-lead",
        "0",
        "-o",
        str(out),
    )
    assert requests[0]["step"] == [0, 3]
    assert requests[0]["param"] == ["tp"]
    import xarray as xr

    written = xr.open_zarr(out)
    assert written.sizes["step"] == 1
    assert list(written.data_vars) == ["precip"]
    assert written["precip"].attrs["units"] == "mm day-1"


def test_resolve_variables_precip_names(mod):
    assert mod._resolve_variables(None, mod.HRES_VARIABLES) == ["precip"]
    for name in ("precip", "tp", "precipitation"):
        assert mod._resolve_variables([name], mod.HRES_VARIABLES) == ["precip"]
    assert list(mod.HRES_VARIABLES)[0] == "precip"


def test_max_lead_parsing(mod):
    assert mod._steps_for(0, mod._parse_max_lead("0")) == [0]
    assert mod._steps_for(0, mod._parse_max_lead("24")) == [0, 3, 6, 9, 12, 15, 18, 21, 24]
    assert mod._steps_for(0, mod._parse_max_lead("1D"))[-1] == 24
    assert mod._steps_for(0, mod._parse_max_lead("156h"))[-2:] == [150, 156]
    assert mod._steps_for(6, None)[-1] == 144
    from weather_skills_core import UsageError

    for bad in ("soon", "-6h"):
        with pytest.raises(UsageError):
            mod._parse_max_lead(bad)


def test_slow_down_is_not_retried_then_aws_is_tried(mod, monkeypatch, tmp_path):
    # ecmwf-opendata retries a "503 Slow Down" (by default 500 times, 120 s
    # apart), which left chats waiting on a throttled source; each source now
    # gets one attempt.
    clients, sources = [], []

    def retrieve(source, request, target):
        sources.append(source)
        if source == "ecmwf":
            raise RuntimeError("503 Server Error: Slow Down for url: https://data.ecmwf.int/...")

    _fake_opendata(monkeypatch, retrieve, clients)
    monkeypatch.setattr(mod, "_open_grib", lambda path: _grib_like_t2m())
    run_skill(
        mod.fetch,
        "--product",
        "hres",
        "--date",
        dt.datetime.now(dt.UTC).date().isoformat(),
        "--bbox",
        "5/34/-5/42",
        "-v",
        "t2m",
        "-o",
        str(tmp_path / "hres.zarr"),
    )
    assert sources == ["ecmwf", "aws"]
    assert [c["maximum_retries"] for c in clients] == [1, 1]


def test_rate_limited_mirror_says_to_try_later(mod, monkeypatch, tmp_path, capsys):
    def retrieve(source, request, target):
        raise RuntimeError(
            "503 Server Error: Service Unavailable for url: https://ecmwf-forecasts.s3..."
        )

    _fake_opendata(monkeypatch, retrieve)
    with pytest.raises(SystemExit):
        run_skill(
            mod.fetch,
            "--product",
            "hres",
            "--date",
            "2026-02-15",
            "--bbox",
            "5/34/-5/42",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert "rate-limiting" in capsys.readouterr().err
    assert not (tmp_path / "x.zarr").exists()


def test_unpublished_init_reports_both_sources(mod, monkeypatch, tmp_path, capsys):
    def retrieve(source, request, target):
        raise RuntimeError(f"404 from {source}")

    _fake_opendata(monkeypatch, retrieve)
    with pytest.raises(SystemExit):
        run_skill(
            mod.fetch,
            "--product",
            "hres",
            "--date",
            dt.datetime.now(dt.UTC).date().isoformat(),
            "--run",
            "18",
            "--bbox",
            "5/34/-5/42",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    err = capsys.readouterr().err
    assert "ecmwf: 404" in err and "aws: 404" in err
    assert "not be published yet" in err
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
        "--product",
        "hres",
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
    assert "cape" not in mod.HRES_VARIABLES
    req = mod._hres_request("2026-10-15", 0, ["mucape"], "sfc", (), [0])
    assert req["param"] == ["mucape"]
    req = mod._hres_request("2026-10-15", 0, ["t"], "pl", mod.HRES_PRESSURE_LEVELS, [0])
    assert "600" in req["levelist"]
    assert {"1000", "50"} <= set(req["levelist"])


def test_run_is_rejected_for_other_products(mod, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            mod.fetch,
            "--date",
            "2026-02-15",
            "--bbox",
            "5/34/-5/42",
            "--run",
            "6",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 2


@pytest.mark.parametrize("extra", [("--list-variables",), ("--date", "2026-02-15")])
def test_hres_rejects_flags_it_cannot_honour(mod, tmp_path, extra):
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "--product", "hres", *extra, "-o", str(tmp_path / "x.zarr"))
    assert exc.value.code == 2


def _mock_inits(monkeypatch, mod, published, requested=None):
    """Open Data mock serving t2m for the `published` init dates (YYYY-MM-DD)."""
    import numpy as np

    last = {}

    def retrieve(source, request, target):
        if requested is not None:
            requested.append(request["date"].isoformat())
        if request["date"].isoformat() not in published:
            raise RuntimeError(f"404 from {source}")
        last["date"] = request["date"]

    def open_grib(path):
        # Each init's value is its day of month, so inits stay distinguishable.
        ds = _grib_like_t2m().assign_coords(time=np.datetime64(last["date"], "ns"))
        return ds.assign(t2m=ds["t2m"] * 0 + 273.15 + last["date"].day)

    _fake_opendata(monkeypatch, retrieve)
    monkeypatch.setattr(mod, "_open_grib", open_grib)


def _run_hres(mod, out, *args):
    run_skill(mod.fetch, "--product", "hres", *args, "--bbox", "5/34/-5/42", "-o", str(out))


def test_init_windows_add_a_time_axis_of_inits(mod, monkeypatch, tmp_path):
    import numpy as np
    import xarray as xr

    requested = []
    _mock_inits(monkeypatch, mod, {"2026-02-14", "2026-02-15", "2026-03-01"}, requested)
    out = tmp_path / "hres.zarr"
    _run_hres(mod, out, "--start-time", "2026-02-14", "--end-time", "2026-02-15",
              "--start-time", "2026-03-01", "--end-time", "2026-03-01", "-v", "t2m")  # fmt: skip
    # Old inits go straight to the AWS mirror: one request per init.
    assert requested == ["2026-02-14", "2026-02-15", "2026-03-01"]
    ds = xr.open_zarr(out)
    assert ds["t2m"].dims == ("time", "step", "latitude", "longitude")
    days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2026-02-14", "2026-02-15", "2026-03-01"]
    np.testing.assert_allclose(ds["t2m"].values[:, 0, 0, 0], [14.0, 15.0, 1.0], rtol=1e-5)
    assert ds["t2m"].attrs["data_interval"] == "3 hour"


def test_overlapping_init_windows_fetch_each_init_once(mod, monkeypatch, tmp_path):
    requested = []
    _mock_inits(monkeypatch, mod, {"2026-02-14", "2026-02-15", "2026-02-16"}, requested)
    _run_hres(mod, tmp_path / "hres.zarr", "--start-time", "2026-02-14", "--end-time",
              "2026-02-15", "--start-time", "2026-02-15", "--end-time", "2026-02-16",
              "-v", "t2m")  # fmt: skip
    assert requested == ["2026-02-14", "2026-02-15", "2026-02-16"]


def test_init_window_skips_an_unpublished_init_with_a_warning(mod, monkeypatch, tmp_path, capsys):
    import xarray as xr

    _mock_inits(monkeypatch, mod, {"2026-02-14", "2026-02-16"})
    out = tmp_path / "hres.zarr"
    _run_hres(mod, out, "--start-time", "2026-02-14", "--end-time", "2026-02-16", "-v", "t2m")
    assert "WARNING: HRES init 2026-02-15 00Z skipped" in capsys.readouterr().err
    days = xr.open_zarr(out)["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2026-02-14", "2026-02-16"]


def test_single_date_keeps_a_scalar_init(mod, monkeypatch, tmp_path):
    import xarray as xr

    _mock_inits(monkeypatch, mod, {"2026-02-15"})
    out = tmp_path / "hres.zarr"
    _run_hres(mod, out, "--date", "2026-02-15", "-v", "t2m")
    ds = xr.open_zarr(out)
    assert ds["t2m"].dims == ("step", "latitude", "longitude")
    assert ds["time"].ndim == 0


@pytest.mark.parametrize(
    "extra",
    [
        ("--date", "2026-02-15", "--start-time", "2026-02-14", "--end-time", "2026-02-15"),
        (),
    ],
)
def test_date_or_init_window_exactly_one_exits_2(mod, monkeypatch, tmp_path, extra):
    _mock_inits(monkeypatch, mod, {"2026-02-15"})
    with pytest.raises(SystemExit) as exc:
        _run_hres(mod, tmp_path / "x.zarr", *extra)
    assert exc.value.code == 2


def test_init_window_with_no_init_exits_1(mod, monkeypatch, tmp_path):
    _mock_inits(monkeypatch, mod, set())
    with pytest.raises(SystemExit) as exc:
        _run_hres(
            mod, tmp_path / "x.zarr", "--start-time", "2026-02-14", "--end-time", "2026-02-15"
        )
    assert exc.value.code == 1
    assert not (tmp_path / "x.zarr").exists()

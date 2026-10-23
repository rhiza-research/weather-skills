"""Correctness tests for hindcast-fetch (bucket stores mocked)."""

import sys
import types

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

PUB = "gs://sheerwater-public-datalake/hindcasts"
PRIV = "gs://sheerwater-datalake/hindcasts"


@pytest.fixture(scope="module")
def mod():
    return load_skill("hindcast-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _store(name, inits, members, value, *, order, lons=(-1.0, 0.0, 1.0), steps=None):
    """One-variable store; value + 0.5 * member index, so the member mean is checkable."""
    lat = np.array([-1.0, 0.0, 1.0], dtype=np.float32)
    lon = np.array(lons, dtype=np.float32)
    coords = {
        "init_time": np.array(inits, dtype="datetime64[ns]"),
        "member": np.array(members, dtype=np.int32),
        "prediction_timedelta": np.arange(3) if steps is None else steps,
        "lat": lat,
        "lon": lon,
    }
    sizes = {d: len(coords[d]) for d in order}
    offset = 0.5 * np.arange(len(members), dtype=np.float32)
    data = np.full([sizes[d] for d in order], value, dtype=np.float32)
    data += np.expand_dims(offset, [i for i, d in enumerate(order) if d != "member"])
    return xr.Dataset({name: (order, data)}, coords=coords)


ECMWF_ORDER = ("member", "prediction_timedelta", "lat", "lon", "init_time")
STORE_ORDER = ("init_time", "member", "prediction_timedelta", "lat", "lon")
EC_INITS = ["2018-10-04", "2018-10-05", "2018-10-08"]
CUM_INITS = ["2024-12-27", "2024-12-29", "2024-12-31"]
DAYS = np.arange(3).astype("timedelta64[D]").astype("timedelta64[ns]")


@pytest.fixture
def stores(mod, monkeypatch):
    ec_p = _store("precip", EC_INITS, [1, 2], 4.0, order=ECMWF_ORDER)
    # The ECMWF store's extra `time` coord repeats init_time.
    ec_p = ec_p.assign_coords(time=("init_time", ec_p["init_time"].values))
    data = {
        f"{PUB}/ecmwf_ifs_precip.zarr": ec_p,
        f"{PUB}/ecmwf_ifs_tmp2m.zarr": _store("tmp2m", EC_INITS, [1, 2], 20.0, order=ECMWF_ORDER),
        f"{PUB}/gefs_precip.zarr": _store(
            "precip", ["2017-10-04", "2017-10-11", "2017-10-18"], [0, 1], 2.0, order=STORE_ORDER
        ),
        f"{PUB}/gefs_tmp2m.zarr": _store("tmp2m", ["2017-10-11"], [0, 1], 25.0, order=STORE_ORDER),
        f"{PRIV}/cumulus_precip.zarr": _store(
            "precip", CUM_INITS, [0, 1, 2], -0.6, order=STORE_ORDER, steps=DAYS
        ),
        f"{PRIV}/cumulus_tmp2m.zarr": _store(
            "tmp2m", CUM_INITS, [0, 1, 2], 300.0, order=STORE_ORDER, steps=DAYS
        ),
    }
    opened = []

    def fake_open(path):
        opened.append(path)
        return data[path]

    monkeypatch.setattr(mod, "_open_store", fake_open)
    return types.SimpleNamespace(data=data, opened=opened)


def _run(fetch, out, *args):
    run_skill(fetch, "--bbox", "2/-2/-2/2", *args, "-o", str(out))


def _dates(ds):
    return ds["time"].values.astype("datetime64[D]").astype(str).tolist()


def test_ecmwf_precip_mean_of_members(tmp_path, fetch, stores):
    out = tmp_path / "ec.zarr"
    _run(fetch, out, "--dataset", "ecmwf", "--start-time", "2018-10-05", "--end-time", "2018-10-31")
    assert stores.opened == [f"{PUB}/ecmwf_ifs_precip.zarr"]
    with xr.open_zarr(out) as ds:
        assert _dates(ds) == ["2018-10-05", "2018-10-08"]
        assert list(ds["precip"].dims) == ["time", "step", "latitude", "longitude"]
        # Members are 4.0 and 4.5 mm: their mean.
        np.testing.assert_allclose(ds["precip"].isel(step=0, latitude=0, longitude=0), [4.25, 4.25])
        assert ds["precip"].attrs["units"] == "mm day-1"
        assert ds["step"].values[1] == np.timedelta64(1, "D")
        assert ds.attrs["weather_skills_source"] == (
            "sheerwater:sheerwater-public-datalake/hindcasts/ecmwf_ifs_precip.zarr"
        )
        assert ds.attrs["hindcast_dataset"] == "ecmwf"
    assert load_history(out)[-1]["skill"] == "hindcast-fetch"


def test_ecmwf_members_and_temperature(tmp_path, fetch, stores):
    out = tmp_path / "ec_members.zarr"
    _run(fetch, out, "--dataset", "ecmwf", "--members", "-v", "precip", "-v", "t2m",
         "--start-time", "2018-10-01", "--end-time", "2018-10-31")  # fmt: skip
    with xr.open_zarr(out) as ds:
        assert list(ds["precip"].dims) == ["time", "number", "step", "latitude", "longitude"]
        assert ds["number"].values.tolist() == [1, 2]
        np.testing.assert_allclose(
            ds["tmp2m"].isel(time=0, step=0, latitude=0, longitude=0), [20.0, 20.5]
        )
        assert ds["tmp2m"].attrs["units"] == "degree_Celsius"


def test_gefs_keeps_inits_missing_from_one_variable(tmp_path, fetch, stores, capsys):
    out = tmp_path / "gefs.zarr"
    _run(fetch, out, "--dataset", "gefs", "-v", "precip", "-v", "tmp2m",
         "--start-time", "2017-10-01", "--end-time", "2017-10-31")  # fmt: skip
    with xr.open_zarr(out) as ds:
        assert _dates(ds) == ["2017-10-04", "2017-10-11", "2017-10-18"]
        assert ds["number"].values.tolist() == [0, 1]
        assert int(ds["tmp2m"].isel(step=0, number=0, latitude=0, longitude=0).notnull().sum()) == 1
    err = capsys.readouterr().err
    assert "gefs tmp2m has no run for 2 init(s), left NaN: 2017-10-04, 2017-10-18" in err


def test_grid_mismatch_exits_1(tmp_path, fetch, stores):
    path = f"{PUB}/gefs_tmp2m.zarr"
    stores.data[path] = stores.data[path].assign_coords(lon=stores.data[path]["lon"] + 1.0)
    with pytest.raises(SystemExit) as exc:
        _run(fetch, tmp_path / "x.zarr", "--dataset", "gefs", "-v", "precip", "-v", "tmp2m",
             "--start-time", "2017-10-01", "--end-time", "2017-10-31")  # fmt: skip
    assert exc.value.code == 1


def test_cumulus_members_negative_precip_kept_and_kelvin_converted(tmp_path, fetch, stores, capsys):
    out = tmp_path / "cum.zarr"
    _run(fetch, out, "--dataset", "cumulus", "-v", "precip", "-v", "tmp2m",
         "--start-time", "2024-12-28", "--end-time", "2024-12-31")  # fmt: skip
    assert "precip goes down to -0.60 mm day-1; kept as published" in capsys.readouterr().err
    with xr.open_zarr(out) as ds:
        assert _dates(ds) == ["2024-12-29", "2024-12-31"]
        assert ds["number"].values.tolist() == [0, 1, 2]
        np.testing.assert_allclose(
            ds["precip"].isel(time=0, step=0, latitude=0, longitude=0), [-0.6, -0.1, 0.4], atol=1e-6
        )
        assert ds["tmp2m"].attrs["units"] == "degree_Celsius"
        t = float(ds["tmp2m"].isel(time=0, number=0, step=0, latitude=0, longitude=0))
        assert t == pytest.approx(26.85, rel=1e-5)


def test_longitudes_wrap_to_plus_minus_180(tmp_path, fetch, stores):
    stores.data[f"{PUB}/gefs_precip.zarr"] = _store(
        "precip", ["2017-10-04"], [0], 1.0, order=STORE_ORDER, lons=(0.0, 1.0, 359.0)
    )
    out = tmp_path / "wrap.zarr"
    run_skill(fetch, "--dataset", "gefs", "--start-time", "2017-10-01", "--end-time", "2017-10-31",
              "-o", str(out))  # fmt: skip
    with xr.open_zarr(out) as ds:
        assert ds["longitude"].values.tolist() == [-1.0, 0.0, 1.0]


def test_window_without_inits_exits_1(tmp_path, fetch, stores):
    with pytest.raises(SystemExit) as exc:
        _run(fetch, tmp_path / "x.zarr", "--dataset", "ecmwf", "--start-time", "1990-01-01",
             "--end-time", "1990-12-31")  # fmt: skip
    assert exc.value.code == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--dataset", "ecmwf", "--start-time", "2018-10-01"],
        ["--dataset", "ecmwf", "--start-time", "2018-10-31", "--end-time", "2018-10-01"],
        ["--dataset", "gefs", "--start-time", "2017-10-01", "--end-time", "2017-10-31", "--members"],
        ["--dataset", "ecmwf", "--start-time", "2018-10-01", "--end-time", "2018-10-31", "-v", "sst"],
        ["--dataset", "cumulus_1deg", "--start-time", "2024-12-01", "--end-time", "2024-12-31"],
        ["--dataset", "ecmwf", "--start-time", "2018-10-01", "--end-time", "2018-10-31",
         "--issue-year", "2022"],
    ],
)  # fmt: skip
def test_usage_errors_exit_2(tmp_path, fetch, stores, args):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, *args, "-o", str(tmp_path / "x.zarr"))
    assert exc.value.code == 2


def test_probe_latest(capsys, fetch, stores):
    run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2018-10-08"
    run_skill(fetch, "--probe-latest", "cumulus")
    assert capsys.readouterr().out.strip() == "2024-12-31"


def test_public_store_opens_anonymously_without_gcsfs(mod, monkeypatch):
    seen = {}

    def fake_open_zarr(path, **kwargs):
        seen.update(path=path, **kwargs)
        return "store"

    monkeypatch.setattr(mod.xr, "open_zarr", fake_open_zarr)
    monkeypatch.setitem(sys.modules, "gcsfs", None)  # importing it would fail
    assert mod._open_store(f"{PUB}/gefs_precip.zarr") == "store"
    assert seen["storage_options"] == {"token": "anon"}


def test_private_store_uses_credentials(mod, monkeypatch):
    seen = {}

    class FakeFS:
        def __init__(self, token):
            seen["token"] = token

        def get_mapper(self, path):
            seen["path"] = path
            return path

    monkeypatch.setitem(sys.modules, "gcsfs", types.SimpleNamespace(GCSFileSystem=FakeFS))
    monkeypatch.setattr(mod.xr, "open_zarr", lambda mapper, **kwargs: "store")
    monkeypatch.setenv("NEURAL_GCM_SERVICE_CREDENTIALS", '{"type": "service_account"}')
    assert mod._open_store(f"{PRIV}/cumulus_precip.zarr") == "store"
    assert seen["token"] == {"type": "service_account"}
    assert seen["path"] == "sheerwater-datalake/hindcasts/cumulus_precip.zarr"


def test_private_store_without_credentials_exits_1(tmp_path, mod, fetch, monkeypatch):
    class FailingFS:
        def __init__(self, token):
            raise RuntimeError("no credentials")

    monkeypatch.setitem(sys.modules, "gcsfs", types.SimpleNamespace(GCSFileSystem=FailingFS))
    monkeypatch.delenv("NEURAL_GCM_SERVICE_CREDENTIALS", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--dataset", "cumulus", "--start-time", "2024-12-01", "--end-time", "2024-12-31",
                  "-o", str(tmp_path / "x.zarr"))  # fmt: skip
    assert exc.value.code == 1

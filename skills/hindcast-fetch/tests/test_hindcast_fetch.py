"""Correctness tests for hindcast-fetch (remote stores mocked)."""

import sys
import types

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

LATS = np.array([-1.5, 0.0, 1.5], dtype=np.float32)
LONS = np.array([-1.5, 0.0, 1.5], dtype=np.float32)
# WeatherBench2 issues -> hindcast years. 2018-10-05 is run by two issues; the
# newest (2021-10-05) is the one the Sheerwater store keeps.
ISSUES = ["2020-10-05", "2021-10-05", "2022-10-03"]
YEARS = np.array([-3, -2, -1], dtype=np.int32)


@pytest.fixture(scope="module")
def mod():
    return load_skill("hindcast-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _inits():
    issued = np.array(ISSUES, dtype="datetime64[D]")
    years = issued.astype("datetime64[Y]").astype(int) + 1970
    return np.array(
        [
            [np.datetime64(f"{y + int(k)}{str(t)[4:]}", "ns") for k in YEARS]
            for t, y in zip(issued, years, strict=True)
        ]
    )


def _value(f, h):
    """Each run's precip in mm: 10 * issue index + |hindcast year|."""
    return 10.0 * f + abs(int(YEARS[h]))


def _wb2(members):
    """WeatherBench2 layout; precip in m, temperatures in K."""
    inits = _inits()
    steps = np.array([0, 1, 2], dtype="timedelta64[D]").astype("timedelta64[ns]")
    precip = (
        np.array([[_value(f, h) for h in range(YEARS.size)] for f in range(len(ISSUES))]) / 1000
    )
    dims = ("forecast_time", "hindcast_year", "prediction_timedelta", "latitude", "longitude")
    shape = (len(ISSUES), YEARS.size, steps.size, LATS.size, LONS.size)
    data = {
        "total_precipitation_24hr": (dims, np.broadcast_to(precip[:, :, None, None, None], shape)),
        "2m_temperature": (dims, np.full(shape, 300.0)),
    }
    coords = {
        "forecast_time": np.array(ISSUES, dtype="datetime64[ns]"),
        "hindcast_year": YEARS,
        "prediction_timedelta": steps,
        "latitude": LATS,
        "longitude": LONS % 360,
        "time": (("forecast_time", "hindcast_year"), inits),
        "valid_time": (("forecast_time", "hindcast_year"), inits),
    }
    ds = xr.Dataset({k: (d, v.astype(np.float32)) for k, (d, v) in data.items()}, coords=coords)
    if members:
        ds = ds.expand_dims(number=[1, 2], axis=1)
    return ds


def _store():
    """Sheerwater ecmwf_ifs_precip: newest issue's run of each init, members 1-2, mm."""
    inits = _inits()
    runs = {}
    for f in range(len(ISSUES)):
        for h in range(YEARS.size):
            runs[inits[f, h]] = _value(f, h)  # later issues overwrite: newest wins
    times = np.array(sorted(runs), dtype="datetime64[ns]")
    member = np.array([0.0, 2.0])  # member offsets; their mean is +1 mm
    values = np.array([runs[t] for t in times])
    precip = member[:, None, None, None, None] + values[None, None, None, None, :]
    precip = np.broadcast_to(precip, (2, 3, LATS.size, LONS.size, times.size)).astype(np.float32)
    return xr.Dataset(
        {"precip": (("member", "prediction_timedelta", "lat", "lon", "init_time"), precip)},
        coords={
            "member": np.array([1, 2], dtype=np.int32),
            "prediction_timedelta": np.arange(3),
            "lat": LATS,
            "lon": LONS,
            "init_time": times,
            "time": ("init_time", times),
        },
    )


@pytest.fixture
def ecmwf(mod, monkeypatch):
    seen = {"store": 0}

    def open_store():
        seen["store"] += 1
        return _store()

    monkeypatch.setattr(mod, "_open_ecmwf", _wb2)
    monkeypatch.setattr(mod, "_open_ecmwf_store", open_store)
    return seen


def _run(fetch, out, *args):
    run_skill(fetch, "--bbox", "2/-2/-2/2", *args, "-o", str(out))


def _dates(ds):
    return ds["time"].values.astype("datetime64[D]").astype(str).tolist()


def test_ecmwf_precip_default_reads_newest_runs_from_store(tmp_path, fetch, ecmwf):
    out = tmp_path / "ec.zarr"
    _run(fetch, out, "--dataset", "ecmwf", "--start-time", "2018-10-01", "--end-time", "2018-10-31")
    assert ecmwf["store"] == 1
    with xr.open_zarr(out) as ds:
        # 2018-10-05 comes from the 2021 issue (year -3), not the 2020 one.
        assert _dates(ds) == ["2018-10-05"]
        assert ds["issue_time"].values.astype("datetime64[D]").astype(str).tolist() == [
            "2021-10-05"
        ]
        assert list(ds["precip"].dims) == ["time", "step", "latitude", "longitude"]
        # Ensemble mean of the store's members: 13 mm + mean offset 1 mm.
        np.testing.assert_allclose(ds["precip"].isel(step=0, latitude=0, longitude=0), [14.0])
        assert ds["precip"].attrs["units"] == "mm day-1"
        assert ds.attrs["weather_skills_source"] == "sheerwater:hindcasts/ecmwf_ifs_precip"
        assert "hindcast_issue_year" not in ds.attrs
    assert load_history(out)[-1]["skill"] == "hindcast-fetch"


def test_ecmwf_issue_year_reads_store_when_it_has_every_run(tmp_path, fetch, ecmwf):
    out = tmp_path / "ec22.zarr"
    _run(
        fetch, out, "--dataset", "ecmwf", "--members", "--issue-year", "2022",
        "--start-time", "2019-01-01", "--end-time", "2021-12-31",
    )  # fmt: skip
    assert ecmwf["store"] == 1
    with xr.open_zarr(out) as ds:
        assert _dates(ds) == ["2019-10-03", "2020-10-03", "2021-10-03"]
        assert ds.attrs["hindcast_issue_year"] == 2022
        assert ds["number"].values.tolist() == [1, 2]
        # Issue index 2, years -3, -2, -1 -> 23, 22, 21 mm (member 1 adds 0).
        np.testing.assert_allclose(
            ds["precip"].sel(number=1).isel(step=0, latitude=0, longitude=0), [23.0, 22.0, 21.0]
        )


def test_ecmwf_issue_year_with_older_runs_reads_weatherbench2(tmp_path, fetch, ecmwf, capsys):
    out = tmp_path / "ec20.zarr"
    # The 2020 issue's 2018-10-05 run is not the store's (2021 ran that date too).
    _run(
        fetch, out, "--dataset", "ecmwf", "--issue-year", "2020",
        "--start-time", "2017-10-01", "--end-time", "2019-10-31",
    )  # fmt: skip
    assert ecmwf["store"] == 0
    with xr.open_zarr(out) as ds:
        assert _dates(ds) == ["2017-10-05", "2018-10-05", "2019-10-05"]
        np.testing.assert_allclose(ds["precip"].isel(step=0, latitude=0, longitude=0), [3, 2, 1])
        assert ds.attrs["weather_skills_source"] == "weatherbench2:ifs-ext-reforecast-ens-mean"
        assert (
            ds["issue_time"].values.astype("datetime64[D]").astype(str).tolist()
            == ["2020-10-05"] * 3
        )


def test_ecmwf_precip_from_store_with_temperature_from_weatherbench2(tmp_path, fetch, ecmwf):
    out = tmp_path / "ecmix.zarr"
    _run(
        fetch, out, "--dataset", "ecmwf", "--issue-year", "2022", "-v", "precip", "-v", "t2m",
        "--start-time", "2020-01-01", "--end-time", "2021-12-31",
    )  # fmt: skip
    assert ecmwf["store"] == 1
    with xr.open_zarr(out) as ds:
        assert _dates(ds) == ["2020-10-03", "2021-10-03"]
        np.testing.assert_allclose(ds["precip"].isel(step=0, latitude=0, longitude=0), [23.0, 22.0])
        np.testing.assert_allclose(
            ds["tmp2m"].isel(step=0, latitude=0, longitude=0), [26.85, 26.85], rtol=1e-5
        )
        assert ds["tmp2m"].attrs["units"] == "degree_Celsius"
        assert ds.attrs["weather_skills_source"] == (
            "sheerwater:hindcasts/ecmwf_ifs_precip+weatherbench2:ifs-ext-reforecast-ens-mean"
        )


def test_ecmwf_before_store_reads_weatherbench2_and_keeps_negative_precip(
    tmp_path, mod, fetch, monkeypatch, capsys
):
    wb2 = _wb2(False)
    wb2["total_precipitation_24hr"][:, :, 1] = -0.005
    monkeypatch.setattr(mod, "_open_ecmwf", lambda members: wb2)
    monkeypatch.setattr(mod, "_open_ecmwf_store", lambda: pytest.fail("read the store"))
    # Pretend the store starts in 2018, so 2017 inits are before it.
    monkeypatch.setattr(mod, "_ECMWF_STORE_START", np.datetime64("2018-01-01", "ns"))
    out = tmp_path / "ec_old.zarr"
    _run(fetch, out, "--dataset", "ecmwf", "--issue-year", "2020", "--start-time", "2017-10-01",
         "--end-time", "2017-10-31")  # fmt: skip
    assert "below -0.1 mm day-1 at lead day(s) [1]" in capsys.readouterr().err
    with xr.open_zarr(out) as ds:
        assert float(ds["precip"].isel(step=1).min()) == pytest.approx(-5.0)


def test_ecmwf_window_no_issue_year_covers_exits_2(tmp_path, fetch, ecmwf):
    with pytest.raises(SystemExit) as exc:
        _run(fetch, tmp_path / "x.zarr", "--dataset", "ecmwf", "--start-time", "1990-01-01",
             "--end-time", "2015-12-31", "-v", "t2m")  # fmt: skip
    assert exc.value.code == 2


def _gefs_store(source, inits, value):
    shape = (len(inits), 2, 3, 2, 2)
    return xr.Dataset(
        {
            source: (
                ("init_time", "member", "prediction_timedelta", "lat", "lon"),
                np.full(shape, value, np.float32),
            )
        },
        coords={
            "init_time": np.array(inits, dtype="datetime64[ns]"),
            "member": np.array([0, 1], np.int32),
            "prediction_timedelta": np.arange(3),
            "lat": np.array([0.0, 1.0], np.float32),
            "lon": np.array([10.0, 11.0], np.float32),
        },
    )


@pytest.fixture
def gefs(mod, monkeypatch):
    stores = {
        "precip": _gefs_store("precip", ["2017-10-04", "2017-10-11", "2017-10-18"], 2.0),
        "tmp2m": _gefs_store("tmp2m", ["2017-10-11"], 25.0),
    }
    monkeypatch.setattr(mod, "_open_gefs", lambda name: stores[name])
    return stores


def _run_gefs(fetch, out, *args):
    run_skill(fetch, "--dataset", "gefs", "--start-time", "2017-10-01", "--end-time", "2017-10-31",
              *args, "-o", str(out))  # fmt: skip


def test_gefs_keeps_inits_missing_from_one_variable(tmp_path, fetch, gefs, capsys):
    out = tmp_path / "gefs.zarr"
    _run_gefs(fetch, out, "-v", "precip", "-v", "tmp2m")
    with xr.open_zarr(out) as ds:
        assert _dates(ds) == ["2017-10-04", "2017-10-11", "2017-10-18"]
        assert list(ds["precip"].dims) == ["time", "number", "step", "latitude", "longitude"]
        assert ds["number"].values.tolist() == [0, 1]
        assert ds["step"].values[1] == np.timedelta64(1, "D")
        assert int(ds["tmp2m"].isel(step=0, number=0, latitude=0, longitude=0).notnull().sum()) == 1
        assert ds["tmp2m"].attrs["units"] == "degree_Celsius"
        assert (
            ds.attrs["weather_skills_source"]
            == "sheerwater:hindcasts/gefs_precip+gefs_chc_raw/tmp2m"
        )
    assert "GEFS tmp2m has no run for 2 init(s)" in capsys.readouterr().err


def test_gefs_grid_mismatch_exits_1(tmp_path, fetch, gefs):
    gefs["tmp2m"] = gefs["tmp2m"].assign_coords(lon=gefs["tmp2m"]["lon"] + 1.0)
    with pytest.raises(SystemExit) as exc:
        _run_gefs(fetch, tmp_path / "x.zarr", "-v", "precip", "-v", "tmp2m")
    assert exc.value.code == 1


def test_gefs_precip_reads_public_store_without_credentials(mod, monkeypatch):
    seen = {}

    def fake_open_zarr(path, **kwargs):
        seen.update(path=path, **kwargs)
        return "store"

    monkeypatch.setattr(mod.xr, "open_zarr", fake_open_zarr)
    monkeypatch.setitem(sys.modules, "gcsfs", None)  # importing it would fail
    assert mod._open_gefs("precip") == "store"
    assert seen["path"] == "gs://sheerwater-public-datalake/hindcasts/gefs_precip.zarr"
    assert seen["storage_options"] == {"token": "anon"}


def _cumulus_file(ens, lead, values):
    """NetCDF bytes in the Cumulus layout; each value gets lead hours / 1000 added."""
    name = "total_precipitation_24h_acc_imerg_0p25"
    mark = int(lead) / 1000
    if ens == "cf":
        var = (("latitude", "longitude"), np.full((2, 2), values[0] + mark, dtype="float32"))
        coords = {}
    else:
        members = np.asarray(values[1:], dtype="float32") + mark
        var = (("number", "latitude", "longitude"), np.repeat(members, 4).reshape(-1, 2, 2))
        coords = {"number": np.arange(1, len(members) + 1)}
    coords.update(
        latitude=np.array([1.0, 0.0], dtype="float32"),
        longitude=np.array([10.0, 11.0], dtype="float32"),
    )
    return xr.Dataset({name: var}, coords=coords).to_netcdf(engine="h5netcdf")


def test_open_cumulus_matches_live_export(mod, monkeypatch):
    asked = []

    class FakeFS:
        def __init__(self, **kwargs):
            pass

        def cat_file(self, path):
            asked.append(path)
            ens = path.split("/gratis-czajkowski/")[1].split("/")[0]
            return _cumulus_file(ens, path[-7:-3], values=(-1.0, 2.0, 3.0))

    monkeypatch.setitem(sys.modules, "adlfs", types.SimpleNamespace(AzureBlobFileSystem=FakeFS))
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", "sv=1&sig=abc")
    da = mod._open_cumulus("total_precipitation_24h_acc_imerg_0p25", "2024-12-31-00", None, 4)
    assert len(asked) == 2 * 46
    assert da.dims == ("time", "step", "number", "latitude", "longitude")
    assert da["number"].values.tolist() == [0, 1, 2]
    hours = da["step"].values.astype("timedelta64[h]").astype(int)
    assert list(hours) == list(range(0, 1081, 24))
    # cf (-1 + lead mark) is clipped to 0 like the live export; step 0 is lead 24 h.
    np.testing.assert_allclose(da.values[0, 0, :, 0, 0], [0.0, 2.024, 3.024], rtol=1e-6)


def test_cumulus_range_stacks_inits(tmp_path, mod, fetch, monkeypatch):
    prefixes, read = [], []
    inits = ["2023-12-31-00", "2024-12-29-00", "2024-12-31-00"]

    def fake_list(folder, prefix):
        prefixes.append(prefix)
        return [i for i in inits if i.startswith(prefix)]

    def fake_open(folder, init, bbox, workers):
        read.append(init)
        data = np.full((1, 2, 3, 2, 2), 4.0, np.float32)
        return xr.DataArray(
            data,
            dims=("time", "step", "number", "latitude", "longitude"),
            coords={
                "time": [np.datetime64(init[:10], "ns")],
                "step": np.array([0, 24], dtype="timedelta64[h]").astype("timedelta64[ns]"),
                "number": [0, 1, 2],
                "latitude": [1.0, 0.0],
                "longitude": [10.0, 11.0],
            },
        )

    monkeypatch.setattr(mod, "_list_cumulus_inits", fake_list)
    monkeypatch.setattr(mod, "_open_cumulus", fake_open)
    out = tmp_path / "cum.zarr"
    run_skill(fetch, "--dataset", "cumulus", "--start-time", "2023-12-01", "--end-time", "2024-12-30",
              "-o", str(out))  # fmt: skip
    assert prefixes == ["2023-", "2024-"]
    assert read == ["2023-12-31-00", "2024-12-29-00"]
    with xr.open_zarr(out) as ds:
        assert list(ds["precip"].dims) == ["time", "number", "step", "latitude", "longitude"]
        assert ds["precip"].attrs["units"] == "mm day-1"
        assert (
            ds.attrs["weather_skills_source"]
            == "cumulus:gratis-czajkowski/total_precipitation_24h_acc_imerg_0p25"
        )


def test_cumulus_empty_range_exits_1(tmp_path, mod, fetch, monkeypatch):
    monkeypatch.setattr(mod, "_list_cumulus_inits", lambda folder, prefix: [])
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--dataset", "cumulus", "--start-time", "2024-12-30", "--end-time", "2024-12-30",
                  "-o", str(tmp_path / "x.zarr"))  # fmt: skip
    assert exc.value.code == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--dataset", "ecmwf", "--start-time", "2018-10-01"],
        ["--dataset", "ecmwf", "--start-time", "2018-10-31", "--end-time", "2018-10-01"],
        [
            "--dataset",
            "gefs",
            "--start-time",
            "2017-10-01",
            "--end-time",
            "2017-10-31",
            "--members",
        ],
        [
            "--dataset",
            "cumulus",
            "--start-time",
            "2024-12-01",
            "--end-time",
            "2024-12-31",
            "--issue-year",
            "2022",
        ],
        [
            "--dataset",
            "cumulus",
            "--start-time",
            "2024-12-01",
            "--end-time",
            "2024-12-31",
            "-v",
            "tmp2m",
        ],
        [
            "--dataset",
            "ecmwf",
            "--start-time",
            "2018-10-01",
            "--end-time",
            "2018-10-31",
            "-v",
            "msl",
        ],
        [
            "--dataset",
            "cumulus",
            "--start-time",
            "2024-12-01",
            "--end-time",
            "2024-12-31",
            "--workers",
            "0",
        ],
    ],
)
def test_usage_errors_exit_2(tmp_path, fetch, ecmwf, args):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch, *args, "-o", str(tmp_path / "x.zarr"))
    assert exc.value.code == 2


def test_probe_latest(capsys, fetch, ecmwf, gefs):
    run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2021-10-03"
    run_skill(fetch, "--probe-latest", "gefs")
    assert capsys.readouterr().out.strip() == "2017-10-18"


@pytest.mark.parametrize(
    "raw",
    [
        "sv=1&sig=abc",
        "?sv=1&sig=abc",
        "https://italynorthdata.blob.core.windows.net/data/x?sv=1&sig=abc",
    ],
)
def test_sas_token_forms(mod, monkeypatch, raw):
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", raw)
    assert mod._azure_options()["sas_token"] == "sv=1&sig=abc"


def test_azure_error_redacts_sas(mod):
    message = str(
        mod._azure_error(RuntimeError("403 https://x/data/a?sv=1&sig=SECRET&se=2"), "failed")
    )
    assert "SECRET" not in message
    assert "sig=REDACTED" in message

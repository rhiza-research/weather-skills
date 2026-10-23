"""Correctness tests for cumulus-fetch (network / remote Zarr mocked)."""

import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

STORES = ["2026-09-07-00.zarr", "2026-09-18-00.zarr"]


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("cumulus-fetch", "fetch")


def _live_store(values=(5.0, 5.0, 5.0), members=3):
    """A store in the live layout: (init_time, lead_time, ensemble_member, lat, lon)."""
    init = np.datetime64("2026-09-18", "ns")
    leads = np.array([np.timedelta64(24 * (i + 1), "h") for i in range(len(values))])
    data = np.broadcast_to(
        np.asarray(values)[None, :, None, None, None], (1, len(values), members, 2, 2)
    ).copy()
    name = "total_precipitation_24h_acc_imerg_0p25"
    ds = xr.Dataset(
        {name: (("init_time", "lead_time", "ensemble_member", "latitude", "longitude"), data)},
        coords={
            "init_time": [init],
            "lead_time": leads.astype("timedelta64[ns]"),
            "ensemble_member": np.arange(members, dtype="int16"),
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
            "valid_time": (("init_time", "lead_time"), (init + leads)[None, :]),
        },
    )
    ds[name].attrs.update(units="mm", long_name="24-hour total precipitation")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    return ds


@pytest.fixture
def remote(fetch_mod, monkeypatch):
    """Mock the bucket: two init stores; record what _open_store was asked for."""
    seen = {}

    def fake_open(url, bbox, workers):
        seen.update(url=url, bbox=bbox, workers=workers)
        return _live_store()

    monkeypatch.setattr(fetch_mod, "_list_stores", lambda folder: list(STORES))
    monkeypatch.setattr(fetch_mod, "_open_store", fake_open)
    return seen


def test_to_standard_maps_live_layout(fetch_mod):
    out = fetch_mod._to_standard(_live_store().squeeze("init_time"))
    assert out["tp"].dims == ("number", "step", "latitude", "longitude")
    assert out["time"].values == np.datetime64("2026-09-18", "ns")
    hours = out["step"].values.astype("timedelta64[h]").astype(int)
    assert list(hours) == [0, 24, 48]
    assert "valid_time" not in out.coords


def test_fetch_writes_latest_init_as_rates(tmp_path, fetch_mod, remote):
    out = tmp_path / "cumulus.zarr"
    run_skill(fetch_mod.fetch, "-o", str(out))

    assert remote["url"].endswith("/zarr/total_precipitation_24h_acc_imerg_0p25/2026-09-18-00.zarr")
    assert Path(out).exists()
    with xr.open_zarr(out, consolidated=True) as ds:
        assert list(ds.data_vars) == ["tp"]
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds["tp"].attrs.get("data_interval")
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 1, 2]
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [5.0, 5.0, 5.0])
        assert ds.attrs["weather_skills_source"] == "cumulus:total_precipitation_24h_acc_imerg_0p25"
    assert load_history(out)[-1]["skill"] == "cumulus-fetch"


def test_fetch_does_not_deaccumulate_period_totals(tmp_path, fetch_mod, monkeypatch, remote):
    monkeypatch.setattr(fetch_mod, "_open_store", lambda *a: _live_store(values=(5.0, 2.0, 0.0)))
    out = tmp_path / "cumulus_period.zarr"
    run_skill(fetch_mod.fetch, "-o", str(out))
    with xr.open_zarr(out, consolidated=True) as ds:
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [5.0, 2.0, 0.0])


def test_fetch_honors_dataset_date_and_bbox(tmp_path, fetch_mod, remote):
    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "precip_1deg",
        "--date",
        "2026-09-07",
        "-v",
        "tp",
        "--bbox",
        "3/9/0/12",
        "--workers",
        "4",
        "-o",
        str(tmp_path / "cumulus_bbox.zarr"),
    )
    assert remote["url"].endswith("/zarr/total_precipitation_24h_acc_imerg/2026-09-07-00.zarr")
    assert remote["bbox"] == (3.0, 9.0, 0.0, 12.0)
    assert remote["workers"] == 4


def test_probe_latest(capsys, fetch_mod, remote):
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-09-18"


def test_unknown_variable_exits_2(tmp_path, fetch_mod, remote):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "-v", "t2m", "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_missing_date_exits_1(tmp_path, fetch_mod, remote):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--date", "2020-01-01", "-o", str(tmp_path / "unused.zarr"))
    assert exc.value.code == 1


@pytest.mark.parametrize(
    "raw",
    [
        "sv=1&sig=abc",
        "?sv=1&sig=abc",
        "  sv=1&sig=abc  ",
        "https://italynorthdata.blob.core.windows.net/data/x?sv=1&sig=abc",
    ],
)
def test_sas_token_forms(fetch_mod, monkeypatch, raw):
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", raw)
    assert fetch_mod._storage_options()["sas_token"] == "sv=1&sig=abc"


def test_azure_error_redacts_sas(fetch_mod):
    exc = RuntimeError("403 https://x.blob.core.windows.net/data/a?sv=1&sig=SECRET&se=2")
    message = str(fetch_mod._azure_error(exc, "failed"))
    assert "SECRET" not in message
    assert "sig=REDACTED" in message
    assert "sp=rl" in message


def test_missing_sas_exits_2(fetch_mod, monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_SAS_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--probe-latest")
    assert exc.value.code == 2


def test_malformed_sas_exits_2(fetch_mod, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", "not-a-sas")
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--probe-latest")
    assert exc.value.code == 2


@pytest.mark.parametrize("workers", ["0", "-1"])
def test_nonpositive_workers_exits_2(tmp_path, fetch_mod, remote, workers):
    # zarr async.concurrency 0 deadlocks the read instead of failing.
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--workers", workers, "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


@pytest.mark.parametrize("flag", ["--dataset", "--probe-latest"])
def test_unknown_dataset_exits_2(tmp_path, fetch_mod, remote, flag):
    # Any other folder's field would be renamed to tp and stamped as precipitation.
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, flag, "2m_temperature", "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_folder_name_accepted_as_dataset(tmp_path, fetch_mod, remote):
    folder = "total_precipitation_24h_acc_era5"
    run_skill(fetch_mod.fetch, "--dataset", folder, "-o", str(tmp_path / "era5.zarr"))
    assert f"/zarr/{folder}/2026-09-18-00.zarr" in remote["url"]


def test_trailing_empty_leads_dropped_and_reported(
    tmp_path, fetch_mod, monkeypatch, remote, capsys
):
    monkeypatch.setattr(
        fetch_mod, "_open_store", lambda *a: _live_store(values=(5.0, np.nan, 2.0, np.nan, np.nan))
    )
    out = tmp_path / "partial.zarr"
    run_skill(fetch_mod.fetch, "-o", str(out))
    assert "WARNING" in capsys.readouterr().err
    with xr.open_zarr(out, consolidated=True) as ds:
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 1, 2]
        # The interior gap stays NaN: never filled.
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [5.0, np.nan, 2.0])


def test_all_empty_store_exits_1(tmp_path, fetch_mod, monkeypatch, remote):
    monkeypatch.setattr(fetch_mod, "_open_store", lambda *a: _live_store(values=(np.nan, np.nan)))
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "-o", str(tmp_path / "empty.zarr"))
    assert exc.value.code == 1


def test_azure_error_redacts_url_encoded_signature(fetch_mod):
    exc = RuntimeError("GET https://x/c?sv=1%26sig%3DAbC%252Bd%253D failed")
    message = str(fetch_mod._azure_error(exc, "failed"))
    assert "AbC" not in message
    assert "sig%3DREDACTED" in message


def _hindcast_file(ens, lead, values):
    """NetCDF bytes in the hindcast layout: pf has members 1..n, cf has none.

    Each value gets lead hours / 1000 added so a lead's position can be checked.
    """
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


def test_open_hindcast_matches_live_layout(fetch_mod, monkeypatch):
    import types

    asked = []

    class FakeFS:
        def __init__(self, **kwargs):
            pass

        def cat_file(self, path):
            asked.append(path)
            ens = path.split("/gratis-czajkowski/")[1].split("/")[0]
            return _hindcast_file(ens, path[-7:-3], values=(-1.0, 2.0, 3.0))

    monkeypatch.setitem(sys.modules, "adlfs", types.SimpleNamespace(AzureBlobFileSystem=FakeFS))
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", "sv=1&sig=abc")
    folder = "total_precipitation_24h_acc_imerg_0p25"
    ds = fetch_mod._open_hindcast(folder, "2024-12-31-00", None, 4)

    assert len(asked) == 2 * 46
    assert asked[0].endswith(f"/cf/{folder}/data/2024-12-31-00-0024.nc")
    da = ds[folder]
    assert da.dims == ("init_time", "lead_time", "ensemble_member", "latitude", "longitude")
    assert list(da["ensemble_member"].values) == [0, 1, 2]
    assert da["init_time"].values[0] == np.datetime64("2024-12-31T00", "ns")
    hours = da["lead_time"].values.astype("timedelta64[h]").astype(int)
    assert list(hours) == list(range(24, 1105, 24))
    # cf (-1 + lead mark) is clipped to 0 like the live export; pf keeps its values.
    np.testing.assert_allclose(da.values[0, 0, :, 0, 0], [0.0, 2.024, 3.024], rtol=1e-6)
    np.testing.assert_allclose(da.values[0, -1, :, 0, 0], [0.104, 3.104, 4.104], rtol=1e-6)
    out = fetch_mod._to_standard(ds)
    assert out["tp"].dims == ("time", "number", "step", "latitude", "longitude")
    assert out["step"].values[0] == np.timedelta64(0, "ns")


@pytest.fixture
def hindcast_remote(fetch_mod, monkeypatch):
    """Mock the hindcast tree: inits in 2023 and 2024; record what was read."""
    seen = {}
    inits = ["2023-12-31-00", "2024-12-29-00", "2024-12-31-00"]

    def fake_list(folder, prefix):
        seen.setdefault("prefixes", []).append(prefix)
        return [i for i in inits if i.startswith(prefix)]

    def fake_open(folder, init, bbox, workers):
        seen.setdefault("inits", []).append(init)
        seen.update(folder=folder, bbox=bbox, workers=workers)
        return _live_store().assign_coords(init_time=[np.datetime64(init[:10], "ns")])

    monkeypatch.setattr(fetch_mod, "_list_hindcast_inits", fake_list)
    monkeypatch.setattr(fetch_mod, "_open_hindcast", fake_open)
    monkeypatch.setattr(fetch_mod, "_list_stores", lambda folder: pytest.fail("listed live"))
    return seen


def test_hindcast_range_stacks_inits_on_time(tmp_path, fetch_mod, hindcast_remote):
    out = tmp_path / "hindcast.zarr"
    run_skill(
        fetch_mod.fetch,
        "--hindcast",
        "--start-time",
        "2023-12-01",
        "--end-time",
        "2024-12-31",
        "-o",
        str(out),
    )
    assert hindcast_remote["prefixes"] == ["2023-", "2024-"]
    assert hindcast_remote["inits"] == ["2023-12-31-00", "2024-12-29-00", "2024-12-31-00"]
    assert hindcast_remote["folder"] == "total_precipitation_24h_acc_imerg_0p25"
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["tp"].dims == ("time", "number", "step", "latitude", "longitude")
        times = np.asarray(ds["time"].values).astype("datetime64[D]").astype(str)
        assert list(times) == ["2023-12-31", "2024-12-29", "2024-12-31"]
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert (
            ds.attrs["weather_skills_source"]
            == "cumulus:gratis-czajkowski/total_precipitation_24h_acc_imerg_0p25"
        )


def test_hindcast_one_init_and_bbox(tmp_path, fetch_mod, hindcast_remote):
    out = tmp_path / "hindcast_bbox.zarr"
    run_skill(
        fetch_mod.fetch,
        "--hindcast",
        "--start-time",
        "2024-12-31",
        "--end-time",
        "2024-12-31",
        "--bbox",
        "3/9/0/12",
        "-o",
        str(out),
    )
    assert hindcast_remote["inits"] == ["2024-12-31-00"]
    assert hindcast_remote["bbox"] == (3.0, 9.0, 0.0, 12.0)
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.sizes["time"] == 1


def test_hindcast_probe_latest(capsys, fetch_mod, hindcast_remote):
    run_skill(fetch_mod.fetch, "--probe-latest", "--hindcast")
    assert capsys.readouterr().out.strip() == "2024-12-31"


def test_hindcast_empty_range_exits_1(tmp_path, fetch_mod, hindcast_remote, capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--hindcast",
            "--start-time",
            "2024-12-30",
            "--end-time",
            "2024-12-30",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 1
    assert "every second day" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--hindcast", "--date", "2024-12-31"],
        ["--hindcast", "--start-time", "2024-12-31"],
        ["--hindcast", "--start-time", "2024-12-31", "--end-time", "2024-12-01"],
        ["--start-time", "2024-12-01", "--end-time", "2024-12-31"],
    ],
)
def test_hindcast_argument_mixups_exit_2(tmp_path, fetch_mod, hindcast_remote, args):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, *args, "-o", str(tmp_path / "x.zarr"))
    assert exc.value.code == 2

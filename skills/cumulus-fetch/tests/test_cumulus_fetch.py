"""Correctness tests for cumulus-fetch (network / remote Zarr mocked)."""

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
    out = fetch_mod._to_standard(_live_store())
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


def test_missing_date_exits_1(tmp_path, fetch_mod, remote, capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--date", "2020-01-01", "-o", str(tmp_path / "unused.zarr"))
    assert exc.value.code == 1
    assert "Nearest published inits: 2026-09-07, 2026-09-18" in capsys.readouterr().err


def test_missing_date_lists_the_inits_around_it(tmp_path, fetch_mod, monkeypatch, capsys):
    """One failed --date shows what is published nearby, so the model need not guess again."""
    stores = [f"2026-09-{d:02d}-00.zarr" for d in (1, 4, 7, 11, 14, 18, 21, 25)]
    monkeypatch.setattr(fetch_mod, "_list_stores", lambda folder: stores)
    with pytest.raises(SystemExit):
        run_skill(fetch_mod.fetch, "--date", "2026-09-12", "-o", str(tmp_path / "unused.zarr"))
    err = capsys.readouterr().err
    assert "Nearest published inits: 2026-09-04, 2026-09-07, 2026-09-11, 2026-09-14," in err
    assert "2026-09-18, 2026-09-21 (8 inits from 2026-09-01 to 2026-09-25)" in err


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


def test_missing_sas_exits_2(fetch_mod, monkeypatch, capsys):
    monkeypatch.delenv("AZURE_STORAGE_SAS_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--probe-latest")
    assert exc.value.code == 2
    assert "Ask the workspace admin" in capsys.readouterr().err


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


def test_hindcast_run_name_says_only_live_runs(tmp_path, fetch_mod, remote, capsys):
    with pytest.raises(SystemExit):
        run_skill(fetch_mod.fetch, "--dataset", "gratis-czajkowski", "--probe-latest")
    assert "hindcast (reforecast) runs are not served here" in capsys.readouterr().err


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


_WINDOW_STORES = [
    "2025-01-30-00.zarr",
    "2025-01-31-00.zarr",
    "2025-02-01-00.zarr",
    "2025-02-01-12.zarr",
    "2026-01-15-00.zarr",
]


def _mock_windows(monkeypatch, fetch_mod, opened=None, short=()):
    """Per-init stores whose tp is the init's day of month; ``short`` inits lack leads 2+."""

    def fake_open(url, bbox, workers):
        name = url.rsplit("/", 1)[-1]
        if opened is not None:
            opened.append(name)
        day = int(name[8:10])
        ds = _live_store(values=(day, day, day) if name not in short else (day, np.nan, np.nan))
        init = np.datetime64(f"{name[:10]}T{name[11:13]}", "ns")
        return ds.drop_vars("valid_time").assign_coords(init_time=[init])

    monkeypatch.setattr(fetch_mod, "_list_stores", lambda folder: list(_WINDOW_STORES))
    monkeypatch.setattr(fetch_mod, "_open_store", fake_open)


def test_init_window_keeps_00utc_inits_on_a_time_axis(tmp_path, fetch_mod, monkeypatch):
    opened = []
    _mock_windows(monkeypatch, fetch_mod, opened)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch, "--start-time", "2025-01-31", "--end-time", "2025-02-01", "-o", str(out)
    )
    # 2025-02-01-12 is not a 00 UTC init.
    assert opened == ["2025-01-31-00.zarr", "2025-02-01-00.zarr"]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["tp"].dims == ("time", "step", "number", "latitude", "longitude")
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
        assert days == ["2025-01-31", "2025-02-01"]
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0, 0, 0], [31.0, 1.0])
    assert load_history(out)[-1]["args"]["start_time"] == ["2025-01-31"]


def test_repeated_init_windows_take_disjoint_inits(tmp_path, fetch_mod, monkeypatch):
    _mock_windows(monkeypatch, fetch_mod)
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch,
        "--start-time",
        "2025-01-01",
        "--end-time",
        "2025-01-31",
        "--start-time",
        "2026-01-01",
        "--end-time",
        "2026-01-31",
        "-o",
        str(out),
    )
    with xr.open_zarr(out, consolidated=True) as ds:
        days = ds["time"].values.astype("datetime64[D]").astype(str).tolist()
    assert days == ["2025-01-30", "2025-01-31", "2026-01-15"]


def test_init_window_keeps_short_init_nan(tmp_path, fetch_mod, monkeypatch, capsys):
    _mock_windows(monkeypatch, fetch_mod, short={"2025-01-31-00.zarr"})
    out = tmp_path / "w.zarr"
    run_skill(
        fetch_mod.fetch, "--start-time", "2025-01-30", "--end-time", "2025-01-31", "-o", str(out)
    )
    assert "Cumulus init 2025-01-31 has no data from step 1 d" in capsys.readouterr().err
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.sizes["step"] == 3
        assert ds["tp"].isel(time=1, step=slice(1, None)).isnull().all()
        assert ds["tp"].isel(time=0).notnull().all()


def test_date_and_init_window_together_exit_2(tmp_path, fetch_mod, monkeypatch):
    _mock_windows(monkeypatch, fetch_mod)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2025-01-31",
            "--start-time",
            "2025-01-31",
            "--end-time",
            "2025-02-01",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 2


def test_init_window_with_no_init_exits_1(tmp_path, fetch_mod, monkeypatch):
    _mock_windows(monkeypatch, fetch_mod)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--start-time",
            "2024-06-01",
            "--end-time",
            "2024-06-30",
            "-o",
            str(tmp_path / "x.zarr"),
        )
    assert exc.value.code == 1

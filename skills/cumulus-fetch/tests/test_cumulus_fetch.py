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

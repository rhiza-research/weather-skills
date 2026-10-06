"""Correctness tests for cumulus-fetch (network / remote Zarr mocked)."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_forecast, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("cumulus-fetch", "fetch")


def _native_forecast(*, fill=5.0, n_step=3, members=3, negatives=False):
    """Source-like cube: native precip name, 24h/48h/72h leads, amount units."""
    remote = make_forecast(
        name="total_precipitation_24h_acc_imerg",
        members=members,
        n_step=n_step,
        fill=fill,
    )
    remote = remote.assign_coords(
        step=("step", np.array([np.timedelta64(h, "h") for h in (24, 48, 72)[:n_step]]))
    )
    remote["total_precipitation_24h_acc_imerg"].attrs.update(units="mm")
    if negatives:
        remote["total_precipitation_24h_acc_imerg"].values[..., 0, 0] = -1.0
    return remote


def test_parse_name(fetch_mod):
    assert fetch_mod._parse_name("2026-09-18-00.zarr") == ("2026-09-18", "00")
    assert fetch_mod._parse_name("2026-09-18-12.zarr") == ("2026-09-18", "12")
    assert fetch_mod._parse_name("2026-09-18-00-0024.nc") is None
    assert fetch_mod._parse_name("readme.txt") is None


def test_data_prefix_points_at_zarr_variable_folder(fetch_mod):
    assert fetch_mod._data_prefix("total_precipitation_24h_acc_imerg_0p25") == (
        "data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/"
        "zarr/total_precipitation_24h_acc_imerg_0p25"
    )


def test_store_url_picks_init(fetch_mod, monkeypatch):
    names = ["2026-09-11-00.zarr", "2026-09-18-00.zarr", "2026-09-18-12.zarr", "x.txt"]
    monkeypatch.setattr(fetch_mod, "_list_objects", lambda dataset: names)
    url = fetch_mod._store_url("v", "2026-09-18")
    assert url.endswith("/zarr/v/2026-09-18-12.zarr")
    assert fetch_mod._init_dates_from_names(names) == ["2026-09-11", "2026-09-18"]
    with pytest.raises(fetch_mod.DataError):
        fetch_mod._store_url("v", "2026-09-25")


def test_normalize_lead_axis_renames_and_drops_init_time(fetch_mod):
    remote = _native_forecast().rename({"step": "lead_time"})
    remote = remote.expand_dims(time=[np.datetime64("2026-09-18", "ns")])
    out = fetch_mod._normalize_lead_axis(remote, "2026-09-18")
    assert "step" in out.dims and "lead_time" not in out.dims
    assert "time" not in out.dims and "time" not in out.coords


def test_normalize_lead_axis_matches_live_store_layout(fetch_mod):
    """Live stores: (init_time, lead_time, ensemble_member, lat, lon) + valid_time."""
    remote = _native_forecast().rename({"step": "lead_time", "number": "ensemble_member"})
    init = np.datetime64("2026-10-06", "ns")
    remote = remote.expand_dims(init_time=[init])
    remote = remote.assign_coords(
        valid_time=(("init_time", "lead_time"), (init + remote["lead_time"].values)[None, :])
    )
    out = fetch_mod._normalize_lead_axis(remote, "2026-10-06")
    assert set(out.dims) == {"number", "step", "latitude", "longitude"}
    assert not {"init_time", "valid_time", "lead_time", "ensemble_member"} & set(out.coords)


def test_normalize_lead_axis_from_valid_time(fetch_mod):
    remote = _native_forecast()
    init = np.datetime64("2026-09-18", "ns")
    remote = remote.assign_coords(time=("step", init + remote["step"].values))
    remote = remote.swap_dims({"step": "time"}).drop_vars("step")
    out = fetch_mod._normalize_lead_axis(remote, "2026-09-18")
    hours = out["step"].values.astype("timedelta64[h]").astype(int)
    assert list(hours) == [24, 48, 72]


def test_canonical_dataset_aliases(fetch_mod):
    assert fetch_mod._canonical_dataset("precip") == "total_precipitation_24h_acc_imerg_0p25"
    assert fetch_mod._canonical_dataset("tp") == "total_precipitation_24h_acc_imerg_0p25"
    assert (
        fetch_mod._canonical_dataset("total_precipitation_24h_acc_imerg_0p25")
        == "total_precipitation_24h_acc_imerg_0p25"
    )
    assert fetch_mod._canonical_dataset("precip_1deg") == "total_precipitation_24h_acc_imerg"
    assert (
        fetch_mod._canonical_dataset("total_precipitation_24h_acc_imerg")
        == "total_precipitation_24h_acc_imerg"
    )


def test_prepare_dataset_renames_0p25_variable(fetch_mod):
    remote = make_forecast(name="total_precipitation_24h_acc_imerg_0p25", members=2, n_step=2)
    prepared = fetch_mod._prepare_dataset(remote, "2026-09-25")
    assert "tp" in prepared.data_vars
    assert "total_precipitation_24h_acc_imerg_0p25" not in prepared.data_vars


def test_fetch_writes_zarr_and_stamps_history(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "cumulus.zarr"
    remote = _native_forecast()

    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda dataset: ["2026-09-07", "2026-09-18"])
    monkeypatch.setattr(fetch_mod, "_open_init", lambda *args, **kwargs: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "-o", str(out))

    assert Path(out).exists()
    with xr.open_zarr(out, consolidated=True) as ds:
        assert "tp" in ds.data_vars
        assert "total_precipitation_24h_acc_imerg" not in ds.data_vars
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds["tp"].attrs.get("data_interval")
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 1, 2]
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [5.0, 5.0, 5.0])
        assert "cumulus:" in ds.attrs.get("weather_skills_source", "")
    history = load_history(out)
    assert history[-1]["skill"] == "cumulus-fetch"


def test_fetch_does_not_deaccumulate_period_totals(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "cumulus_period.zarr"
    remote = _native_forecast(fill=0.0)
    for i, val in enumerate((5.0, 2.0, 0.0)):
        remote["total_precipitation_24h_acc_imerg"].values[:, i, :, :] = val

    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda dataset: ["2026-09-18"])
    monkeypatch.setattr(fetch_mod, "_open_init", lambda *args, **kwargs: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--date", "2026-09-18", "-o", str(out))

    with xr.open_zarr(out, consolidated=True) as ds:
        np.testing.assert_allclose(ds["tp"].values[0, :, 0, 0], [5.0, 2.0, 0.0])
        assert ds["tp"].attrs["units"] == "mm day-1"


def test_fetch_clips_negatives(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "cumulus_clip.zarr"
    remote = _native_forecast(negatives=True)

    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda dataset: ["2026-09-18"])
    monkeypatch.setattr(fetch_mod, "_open_init", lambda *args, **kwargs: remote.copy(deep=True))

    run_skill(fetch_mod.fetch, "--date", "2026-09-18", "-o", str(out))

    with xr.open_zarr(out, consolidated=True) as ds:
        assert float(ds["tp"].values.min()) >= 0.0


def test_fetch_honors_date_variable_and_bbox(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "cumulus_bbox.zarr"
    remote = _native_forecast()
    seen = {}

    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda dataset: ["2026-09-18"])

    def fake_open(dataset, iso, bbox, workers):
        seen["dataset"] = dataset
        seen["iso"] = iso
        seen["bbox"] = bbox
        seen["workers"] = workers
        return remote.copy(deep=True)

    monkeypatch.setattr(fetch_mod, "_open_init", fake_open)

    run_skill(
        fetch_mod.fetch,
        "--dataset",
        "precip",
        "--date",
        "2026-09-18",
        "-v",
        "tp",
        "--bbox",
        "3/9/0/12",
        "-o",
        str(out),
    )

    assert seen["dataset"] == "total_precipitation_24h_acc_imerg_0p25"
    assert seen["iso"] == "2026-09-18"
    assert seen["bbox"] == (3.0, 9.0, 0.0, 12.0)
    ds = xr.open_zarr(out, consolidated=True)
    assert list(ds.data_vars) == ["tp"]


def test_probe_latest(capsys, fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda dataset: ["2026-09-07", "2026-09-18"])
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-09-18"


def test_unknown_variable_exits_2(tmp_path, fetch_mod, monkeypatch):
    remote = _native_forecast()
    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda dataset: ["2026-09-18"])
    monkeypatch.setattr(fetch_mod, "_open_init", lambda *args, **kwargs: remote.copy(deep=True))
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2026-09-18",
            "-v",
            "t2m",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_normalize_sas(fetch_mod):
    assert fetch_mod._normalize_sas("?sv=1&sig=abc") == "sv=1&sig=abc"
    assert fetch_mod._normalize_sas("  sv=1&sig=abc  ") == "sv=1&sig=abc"
    url = "https://italynorthdata.blob.core.windows.net/data/x?sv=1&sig=abc"
    assert fetch_mod._normalize_sas(url) == "sv=1&sig=abc"


def test_redact_sas(fetch_mod):
    text = "403 https://x.blob.core.windows.net/data/a?sv=1&sig=SECRET&se=2"
    redacted = fetch_mod._redact(text)
    assert "SECRET" not in redacted
    assert "sig=REDACTED" in redacted


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


def test_missing_date_exits_1(tmp_path, fetch_mod, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda dataset: ["2026-09-07"])
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--date",
            "2020-01-01",
            "-o",
            str(tmp_path / "unused.zarr"),
        )
    assert exc.value.code == 1

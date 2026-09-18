"""Correctness tests for cumulus-fetch (network / remote NetCDF mocked)."""

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
    assert fetch_mod._parse_name("2026-09-18-00-0024.nc") == ("2026-09-18", "00", 24)
    assert fetch_mod._parse_name("2026-09-18-00-1104.nc") == ("2026-09-18", "00", 1104)
    assert fetch_mod._parse_name("readme.txt") is None


def test_canonical_dataset_aliases(fetch_mod):
    assert fetch_mod._canonical_dataset("precip") == "total_precipitation_24h_acc_imerg"
    assert fetch_mod._canonical_dataset("tp") == "total_precipitation_24h_acc_imerg"
    assert (
        fetch_mod._canonical_dataset("total_precipitation_24h_acc_imerg")
        == "total_precipitation_24h_acc_imerg"
    )


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

    assert seen["dataset"] == "total_precipitation_24h_acc_imerg"
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

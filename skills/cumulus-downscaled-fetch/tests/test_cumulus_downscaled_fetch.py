"""Correctness tests for cumulus-downscaled-fetch (Azure listing and file reads mocked)."""

import sys
import types

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

ROOT = "data/live_forecasts/downscaled_models/aurora/east_africa-ghana-kenya-senegal-west_africa"
DIFF = "model_diffusion_unet_precip_input_output_residual_shiftedlog_calibrated_silu_selfcond_precip24h_only"
UNET = "model_unet_precip_output_residual_fs128"
ERA5 = "total_precipitation_24h_acc_era5"


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("cumulus-downscaled-fetch", "fetch")


def _lead_file(init, hours, value):
    """One lead file in the live layout: precip_24h (time, latitude, longitude), latitude descending."""
    init = np.datetime64(init, "ns")
    lead = np.timedelta64(hours, "h")
    return xr.Dataset(
        {"precip_24h": (("time", "latitude", "longitude"), np.full((1, 3, 2), value, "float32"))},
        coords={
            "time": [init + lead],
            "lead_time": ("time", [lead]),
            "latitude": np.array([1.0, 0.9, 0.8], "float32"),
            "longitude": np.array([36.0, 36.1], "float32"),
            "init_time": init,
        },
    )


def _tree(leads=(24, 48, 72), ckpts=("ckpt-a",), members=2):
    """A fake container: {path: children}. Two inits; the newest has no unet output."""
    tree = {ROOT: ["2026-10-20", "2026-10-27", "not-a-date"]}
    for init in ("2026-10-20", "2026-10-27"):
        models = [DIFF] + ([UNET] if init == "2026-10-20" else [])
        tree[f"{ROOT}/{init}"] = models
        for model in models:
            tree[f"{ROOT}/{init}/{model}"] = list(ckpts)
            for ckpt in ckpts:
                tree[f"{ROOT}/{init}/{model}/{ckpt}"] = [ERA5]
                data = f"{ROOT}/{init}/{model}/{ckpt}/{ERA5}/precip_24h/data"
                names = [f"{init}-00-{h:04d}.nc" for h in leads]
                dirs = [f"member_{m:03d}" for m in range(members)] if model == DIFF else []
                tree[data] = names + dirs + ["notes.yml"]
                for d in dirs:
                    tree[f"{data}/{d}"] = names
    return tree


@pytest.fixture
def remote(fetch_mod, monkeypatch):
    """Mock Azure: listing from a fake tree; each file's value = member number + 1 (mean: 5)."""
    state = {"tree": _tree(), "read": []}

    class FakeFS:
        def ls(self, path, detail=False):
            if path not in state["tree"]:
                raise FileNotFoundError(path)
            return [f"{path}/{name}" for name in state["tree"][path]]

        def cat_file(self, path):
            state["read"].append(path)
            return path.encode()

    def fake_open_dataset(buf, engine):
        parent, name = buf.getvalue().decode().rsplit("/", 1)
        member = parent.rsplit("/", 1)[-1]
        value = int(member[7:]) + 1 if member.startswith("member_") else 5.0
        return _lead_file(name[:10], int(name[-7:-3]), value)

    monkeypatch.setattr(fetch_mod, "_filesystem", FakeFS)
    monkeypatch.setattr(xr, "open_dataset", fake_open_dataset)
    return state


def test_fetch_latest_ensemble_mean_as_rates(tmp_path, fetch_mod, remote, capsys):
    out = tmp_path / "ds.zarr"
    run_skill(fetch_mod.fetch, "-o", str(out))
    assert all("/2026-10-27/" in p and "member_" not in p for p in remote["read"])
    with xr.open_zarr(out, consolidated=True) as ds:
        assert list(ds.data_vars) == ["tp"]
        assert ds["tp"].dims == ("step", "latitude", "longitude")
        assert ds["tp"].attrs["units"] == "mm day-1"
        days = np.asarray(ds["step"].values).astype("timedelta64[D]").astype(int)
        assert list(days) == [0, 1, 2]
        assert ds["time"].values == np.datetime64("2026-10-27", "ns")
        assert list(ds["latitude"].values) == pytest.approx([0.8, 0.9, 1.0])
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0], [5.0, 5.0, 5.0])
        assert ds.attrs["weather_skills_source"] == f"cumulus-downscaled:{DIFF}/ckpt-a/{ERA5}"
        assert ds.attrs["cumulus_downscaled_member_set"] == "ensemble_mean"
    assert load_history(out)[-1]["skill"] == "cumulus-downscaled-fetch"
    # Only a short run warns.
    assert "of 46 daily leads" in capsys.readouterr().err


def test_members_stack_on_number(tmp_path, fetch_mod, remote):
    out = tmp_path / "members.zarr"
    run_skill(fetch_mod.fetch, "--members", "-o", str(out))
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["tp"].dims == ("number", "step", "latitude", "longitude")
        assert list(ds["number"].values) == [0, 1]
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0, 0], [1.0, 2.0])


def test_model_without_latest_init_walks_back(tmp_path, fetch_mod, remote, capsys):
    run_skill(fetch_mod.fetch, "--probe-latest", "unet")
    assert capsys.readouterr().out.strip() == "2026-10-20"
    run_skill(fetch_mod.fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-10-27"


def test_members_on_single_field_model_exits_1(tmp_path, fetch_mod, remote):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--model", "unet", "--members", "-o", str(tmp_path / "x.zarr"))
    assert exc.value.code == 1


def test_missing_interior_lead_stays_nan(tmp_path, fetch_mod, remote, capsys):
    remote["tree"] = _tree(leads=(24, 72))
    out = tmp_path / "gap.zarr"
    run_skill(fetch_mod.fetch, "-o", str(out))
    assert "step(s) 1 d have no file" in capsys.readouterr().err
    with xr.open_zarr(out, consolidated=True) as ds:
        values = ds["tp"].values[:, 0, 0]
        assert values[0] == 5.0 and np.isnan(values[1]) and values[2] == 5.0


def test_several_checkpoints_exit_1_until_one_is_picked(tmp_path, fetch_mod, remote, capsys):
    remote["tree"] = _tree(ckpts=("ckpt-n20_cfg2", "ckpt-n20_cfg3"))
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "-o", str(tmp_path / "a.zarr"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ckpt-n20_cfg2, ckpt-n20_cfg3" in err and "--checkpoint" in err
    assert remote["read"] == []
    run_skill(fetch_mod.fetch, "--checkpoint", "cfg2", "-o", str(tmp_path / "b.zarr"))
    assert "Checkpoint: ckpt-n20_cfg2" in capsys.readouterr().err
    assert remote["read"] and all("ckpt-n20_cfg2" in p for p in remote["read"])


def test_several_checkpoints_keep_the_one_a_later_init_runs(tmp_path, fetch_mod, remote, capsys):
    remote["tree"] = _tree(ckpts=("ckpt-n20_cfg2", "ckpt-n20_cfg3"))
    remote["tree"][f"{ROOT}/2026-10-27/{DIFF}"] = ["ckpt-n20_cfg2"]
    run_skill(fetch_mod.fetch, "--date", "2026-10-20", "-o", str(tmp_path / "a.zarr"))
    err = capsys.readouterr().err
    assert "Using ckpt-n20_cfg2, the one init 2026-10-27 still runs" in err
    assert remote["read"] and all("ckpt-n20_cfg2" in p for p in remote["read"])


def test_checkpoint_missing_at_date_lists_what_is_there(tmp_path, fetch_mod, remote, capsys):
    remote["tree"] = _tree(ckpts=("ckpt-n30_cfg2",))
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            *("--date", "2026-10-27", "--checkpoint", "n20_cfg2", "-o", str(tmp_path / "x.zarr")),
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "That init has only: ckpt-n30_cfg2" in err and "different setup" in err


def test_setup_other_than_the_latest_warns(tmp_path, fetch_mod, remote, capsys):
    remote["tree"] = _tree(ckpts=("ckpt-n20_cfg2", "ckpt-n20_cfg3"))
    remote["tree"][f"{ROOT}/2026-10-27/{DIFF}"] = ["ckpt-n20_cfg2"]
    args = ("--date", "2026-10-20", "--checkpoint", "cfg3", "-o", str(tmp_path / "a.zarr"))
    run_skill(fetch_mod.fetch, *args)
    assert "differs from the one the latest init 2026-10-27 runs" in capsys.readouterr().err
    run_skill(fetch_mod.fetch, "--date", "2026-10-20", "-o", str(tmp_path / "b.zarr"))
    assert "differs from" not in capsys.readouterr().err


def test_bbox_subsets(tmp_path, fetch_mod, remote):
    out = tmp_path / "bbox.zarr"
    run_skill(fetch_mod.fetch, "--bbox", "1/36/0.85/36.05", "-o", str(out))
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.sizes["latitude"] == 2 and ds.sizes["longitude"] == 1


def test_missing_date_exits_1(tmp_path, fetch_mod, remote):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--date", "2020-01-01", "-o", str(tmp_path / "unused.zarr"))
    assert exc.value.code == 1


def test_unknown_variable_exits_2(tmp_path, fetch_mod, remote):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "-v", "t2m", "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "raw",
    [
        "sv=1&sig=abc",
        "?sv=1&sig=abc",
        "https://italynorthdata.blob.core.windows.net/data/x?sv=1&sig=abc",
    ],
)
def test_sas_token_forms(fetch_mod, monkeypatch, raw):
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", raw)
    monkeypatch.setitem(sys.modules, "adlfs", types.SimpleNamespace(AzureBlobFileSystem=dict))
    assert fetch_mod._filesystem()["sas_token"] == "sv=1&sig=abc"


def test_azure_error_redacts_sas(fetch_mod):
    exc = RuntimeError("403 https://x.blob.core.windows.net/data/a?sv=1&sig=SECRET&se=2")
    message = str(fetch_mod._azure_error(exc, "failed"))
    assert "SECRET" not in message
    assert "sig=REDACTED" in message


def test_missing_sas_exits_2(fetch_mod, monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_SAS_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--probe-latest")
    assert exc.value.code == 2

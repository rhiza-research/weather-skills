"""The verifier must reject structurally plausible but scientifically wrong outputs (spec:
anti-reward-hacking). One oracle run of a weekly-totals scenario, then one mutation per test.
Every mutation keeps the file name and opens as a valid Zarr, so only the checks can catch it."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import xarray as xr

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from gen_scenarios import OUT, chain_for, run_chain  # noqa: E402
from run_eval import score, seed_fixture  # noqa: E402
from weather_skills_core.provenance import HISTORY_ATTR  # noqa: E402

PARAMS = {
    "fixture": {
        "kind": "daily_rates",
        "path": "rates.zarr",
        "n_time": 22,
        "start": "2026-08-01",
        "name": "precip",
        "fill": 3.0,
    },
    "end": "2026-08-22",
}


@pytest.fixture(scope="module")
def oracle(tmp_path_factory):
    w = tmp_path_factory.mktemp("oracle")
    seed_fixture(w, PARAMS["fixture"])
    chain = chain_for("weekly_totals", PARAMS)
    run_chain(w, chain)
    with xr.open_zarr(w / OUT, consolidated=True) as ds:
        mean = float(ds["precip"].mean().compute())
        expect = {
            "skills_used": sorted({s for s, _ in chain}),
            "outputs": [
                {
                    "glob": OUT,
                    "min_count": 1,
                    "checks": {
                        "has_history": True,
                        "dim_sizes": {"time": int(ds.sizes["time"])},
                        "var_units": {"precip": [ds["precip"].attrs.get("units")]},
                        "value_mean": {"precip": [mean, 1e-4 * abs(mean)]},
                    },
                }
            ],
        }
    return w, expect


def _copy(oracle, tmp_path) -> Path:
    w, _ = oracle
    dst = tmp_path / "w"
    shutil.copytree(w, dst)
    return dst


def _rewrite(path: Path, fn):
    ds = xr.open_zarr(path, consolidated=True).load()
    ds = fn(ds)
    shutil.rmtree(path)
    ds.to_zarr(path, mode="w", consolidated=True)


def _passes(w, expect) -> bool:
    return all(ok for ok, _ in score(w, expect))


def test_oracle_itself_passes(oracle):
    w, expect = oracle
    assert _passes(w, expect), [r for r in score(w, expect) if not r[0]]


def test_units_changed_values_unchanged(oracle, tmp_path):
    w = _copy(oracle, tmp_path)
    _rewrite(w / OUT, lambda ds: ds.assign(precip=ds["precip"].assign_attrs(units="mm day-1")))
    assert not _passes(w, oracle[1])


def test_mean_reported_as_total(oracle, tmp_path):
    w = _copy(oracle, tmp_path)
    _rewrite(
        w / OUT, lambda ds: ds.assign(precip=(ds["precip"] / 7).assign_attrs(ds["precip"].attrs))
    )
    assert not _passes(w, oracle[1])


def test_bin_dropped(oracle, tmp_path):
    w = _copy(oracle, tmp_path)
    _rewrite(
        w / OUT,
        lambda ds: (
            ds.isel(time=slice(0, -1)) if ds.sizes["time"] > 1 else ds.isel(time=slice(0, 0))
        ),
    )
    assert not _passes(w, oracle[1])


def test_provenance_stripped(oracle, tmp_path):
    w = _copy(oracle, tmp_path)

    def strip(ds):
        ds.attrs.pop(HISTORY_ATTR, None)
        for v in ds.variables.values():
            v.attrs.pop(HISTORY_ATTR, None)
        return ds

    _rewrite(w / OUT, strip)
    for p in (w / "out").glob("*.zarr"):
        if p.name != Path(OUT).name:
            shutil.rmtree(
                p
            )  # also remove intermediates, so skills_used cannot be satisfied by them
    assert not _passes(w, oracle[1])


def test_fixture_copied_as_answer(oracle, tmp_path):
    w = _copy(oracle, tmp_path)
    shutil.rmtree(w / "out")
    (w / "out").mkdir()
    shutil.copytree(w / "rates.zarr", w / OUT)
    assert not _passes(w, oracle[1])


def test_forged_provenance_on_copied_fixture(oracle, tmp_path):
    """History claims the right skills, but the numbers are the untouched fixture."""
    w = _copy(oracle, tmp_path)
    forged = xr.open_zarr(w / OUT, consolidated=True).attrs.get(HISTORY_ATTR)
    shutil.rmtree(w / OUT)
    shutil.copytree(w / "rates.zarr", w / OUT)
    _rewrite(w / OUT, lambda ds: ds.assign_attrs({HISTORY_ATTR: forged}) if forged else ds)
    assert not _passes(w, oracle[1])


def test_expect_cannot_rejects_improvised_output(tmp_path):
    (tmp_path / "_eval").mkdir()
    (tmp_path / "_eval" / "usage.json").write_text(json.dumps({"status": "done"}))
    (tmp_path / "out").mkdir()
    xr.Dataset({"x": ("t", [1.0])}).to_zarr(tmp_path / "out" / "result.zarr", consolidated=True)
    assert not all(ok for ok, _ in score(tmp_path, {"expect_cannot": True}))

"""Predicates vs the REAL skill code on fixtures.

Invariant: if a verdict carries a reason enforced_by="code", the skill must refuse; if the only
reasons are enforced_by="layer", the skill must RUN (that is exactly the silent-failure case the
layer exists for); if eligible, the skill must run.
"""

import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr

from agentic.predicates import evaluate
from agentic.state import artifact_state, canonical_state

AGG = load_skill("aggregate-temporal", "aggregate").aggregate
TOT = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals
S2T = load_skill("step-to-time", "step_to_time").step_to_time
DEA = load_skill("deaccumulate", "deaccumulate").deaccumulate
DIF = load_skill("difference", "difference").difference
VER = load_skill("verify", "verify").verify


def rate_fc(n_step=14, members=2):
    ds = make_forecast(n_step=n_step, members=members)
    ds["tp"].attrs.update(units="mm day-1", long_name="Total precipitation rate")
    return ds


def runs(fn, *argv) -> bool:
    try:
        run_skill(fn, *argv)
        return True
    except (Exception, SystemExit):
        return False


def state(scale="weekly", **arts):
    obj = {"intent": "test", "temporal_scale": scale, "region": None}
    return canonical_state(obj, {k: artifact_state(v, role=k) for k, v in arts.items()})


def check(verdict, ran):
    code = [r for r in verdict.reasons if r.enforced_by == "code"]
    if code:
        assert not ran, (
            f"{verdict.action}: predicate says code refuses ({code[0].code}) but skill ran"
        )
    else:
        assert ran, f"{verdict.action}: predicate allows (or layer-only) but skill refused"


@pytest.fixture
def z(tmp_path):
    n = [0]

    def _z(ds=None):
        n[0] += 1
        p = tmp_path / f"a{n[0]}.zarr"
        if ds is not None:
            write_zarr(ds, p)
        return str(p)

    return _z


def test_convert_to_totals_requires_aggregation(z):
    ds = rate_fc()
    check(evaluate(state(forecast=ds), "convert-to-totals"), runs(TOT, "-i", z(ds), "-o", z()))


def test_convert_to_totals_after_aggregation(z):
    out = z()
    assert runs(AGG, "-i", z(rate_fc()), "-o", out, "--period", "weekly")
    agg = xr.open_zarr(out)
    check(evaluate(state(forecast=agg), "convert-to-totals"), runs(TOT, "-i", out, "-o", z()))


@pytest.mark.parametrize("period", ["weekly", "monthly"])
def test_aggregate_on_lead_axis(z, period):
    ds = rate_fc(n_step=62)
    check(
        evaluate(state(scale=period, forecast=ds), "aggregate-temporal"),
        runs(AGG, "-i", z(ds), "-o", z(), "--period", period),
    )


@pytest.mark.parametrize("rate", [True, False])
def test_deaccumulate(z, rate):
    ds = rate_fc(n_step=5) if rate else make_forecast(n_step=5, members=2)
    check(evaluate(state(forecast=ds), "deaccumulate"), runs(DEA, "-i", z(ds), "-o", z()))


@pytest.mark.parametrize("forecast", [True, False])
def test_step_to_time(z, forecast):
    ds = rate_fc(n_step=5) if forecast else make_gridded(n_time=5)
    check(evaluate(state(x=ds), "step-to-time"), runs(S2T, "-i", z(ds), "-o", z()))


@pytest.mark.parametrize("same_grid", [True, False])
def test_difference_grid_rule_is_layer_only(z, same_grid):
    a = make_gridded(n_time=3)
    b = (
        make_gridded(n_time=3)
        if same_grid
        else make_gridded(n_time=3, lats=(1.0, 1.5, 2.0, 2.5), lons=(10.0, 10.5, 11.0, 11.5))
    )
    v = evaluate(state(a=a, b=b), "difference")
    ran = runs(DIF, "-i", z(a), "-i", z(b), "-o", z())
    check(v, ran)
    if not same_grid:
        assert not v.eligible and ran, "the silent-failure case: skill runs, layer must refuse"


@pytest.mark.parametrize("case", ["lead_vs_time", "grid_mismatch", "ok"])
def test_verify(z, case):
    obs = make_gridded(n_time=3, lats=(1.0, 2.0), lons=(10.0, 11.0), name="tp")
    if case == "lead_vs_time":
        fc = rate_fc(n_step=3, members=None)
    elif case == "grid_mismatch":
        fc = make_gridded(n_time=3, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0), name="tp")
    else:
        fc = make_gridded(n_time=3, lats=(1.0, 2.0), lons=(10.0, 11.0), name="tp")
    v = evaluate(state(forecast=fc, observation=obs), "verify")
    check(v, runs(VER, "--forecast", z(fc), "--obs", z(obs), "-o", z(), "--metric", "bias"))


def test_verify_accepts_near_equal_spacing_like_the_code(z):
    """verify tolerates a 1% spacing difference; the predicate must not be stricter."""
    obs = make_gridded(n_time=3, lats=(1.0, 2.0), lons=(10.0, 11.0), name="tp")
    fc = make_gridded(n_time=3, lats=(1.0, 2.005), lons=(10.0, 11.005), name="tp")
    v = evaluate(state(forecast=fc, observation=obs), "verify")
    assert v.eligible
    check(v, runs(VER, "--forecast", z(fc), "--obs", z(obs), "-o", z(), "--metric", "bias"))


def test_credentials_follow_the_code_not_the_docs(tmp_path):
    s = state(x=make_gridded(n_time=3))
    # ecmwf-fetch defaults its URL; only the key is required
    assert evaluate(s, "ecmwf-fetch", env={"ECMWF_DATASTORES_KEY": "k"}).eligible
    assert any(r.code == "MISSING_CREDENTIAL" for r in evaluate(s, "ecmwf-fetch", env={}).reasons)
    # smap-fetch: env vars OR a netrc entry
    netrc = tmp_path / "netrc"
    netrc.write_text("machine urs.earthdata.nasa.gov login u password p\n")
    assert not [r for r in evaluate(s, "smap-fetch", env={"NETRC": str(netrc)}).reasons
                if r.code == "MISSING_CREDENTIAL"]  # fmt: skip
    none = tmp_path / "missing"
    assert [r for r in evaluate(s, "smap-fetch", env={"NETRC": str(none)}).reasons
            if r.code == "MISSING_CREDENTIAL"]  # fmt: skip

"""Correctness tests for pbc-correct."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr

LATS, LONS = [1.0, 2.0], [10.0, 11.0]
INIT = pd.Timestamp("2026-06-01")
STEP = 14


@pytest.fixture(scope="module")
def pbc_fn():
    return load_skill("pbc-correct", "pbc_correct").pbc_correct


def _obs(values_for):
    """Daily-labeled 7-day window means from 2000 to 2026; values_for(dates) -> (T, 2, 2)."""
    dates = pd.date_range("2000-01-01", "2026-12-31", freq="D")
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), values_for(dates))},
        coords={"time": dates, "latitude": LATS, "longitude": LONS},
    )
    ds["precip"].attrs.update(units="mm day-1", aggregation_period="7 day")
    return ds


def _ensemble(inits, members_for, n_members=3):
    """Weekly-window ensemble: members_for(valid_date) -> (members, 2, 2)."""
    inits = pd.DatetimeIndex(inits)
    lead = pd.Timedelta(days=STEP)
    data = np.stack([members_for(t + lead) for t in inits])[:, None]
    ds = xr.Dataset(
        {"tp": (("time", "step", "number", "latitude", "longitude"), data)},
        coords={
            "time": inits,
            "step": [lead],
            "number": np.arange(n_members),
            "latitude": LATS,
            "longitude": LONS,
        },
    )
    ds["tp"].attrs.update(units="mm day-1", aggregation_period="7 day")
    return ds


def _hindcast_inits(first=2005, last=2025):
    """Weekly inits from mid-April to mid-July in each hindcast year."""
    return pd.DatetimeIndex(
        np.concatenate(
            [
                pd.date_range(f"{y}-04-15", f"{y}-07-15", freq="7D").values
                for y in range(first, last + 1)
            ]
        )
    )


def _run(pbc_fn, tmp_path, fc, hc, obs, *extra):
    out = tmp_path / "pbc.zarr"
    run_skill(
        pbc_fn,
        "--forecast", str(write_zarr(fc, tmp_path / "fc.zarr")),
        "--hindcast", str(write_zarr(hc, tmp_path / "hc.zarr")),
        "--obs", str(write_zarr(obs, tmp_path / "obs.zarr")),
        "-v", "tp",
        *extra,
        "-o", str(out),
    )  # fmt: skip
    return xr.open_zarr(out, consolidated=True).load()


def _random_obs(seed=0):
    rng = np.random.default_rng(seed)
    return _obs(lambda d: rng.gamma(2.0, 2.0, (len(d), 2, 2)))


def _obs_at(obs, date):
    return obs["precip"].sel(time=date).values


def test_raw_puts_all_probability_on_the_observed_quintile(tmp_path, pbc_fn):
    obs = _random_obs()
    members = lambda v: np.repeat(_obs_at(obs, v)[None], 3, axis=0)  # noqa: E731
    fc = _ensemble([INIT], members)
    hc = _ensemble(_hindcast_inits(), members)

    ds = _run(pbc_fn, tmp_path, fc, hc, obs.rename(precip="tp"), "--components", "raw")

    valid = INIT + pd.Timedelta(days=STEP)
    assert pd.Timestamp(ds["time"].values[0]) == valid
    assert pd.Timestamp(ds["forecast_reference_time"].values[0]) == INIT
    np.testing.assert_allclose(ds["tp"].sum("quintile"), 1.0, atol=1e-6)
    edges = ds["quintile_threshold"].isel(time=0).values
    y = _obs_at(obs, valid)
    expected = (y[None] <= edges).argmax(axis=0)
    np.testing.assert_array_equal(ds["tp"].isel(time=0).argmax("quintile").values, expected)
    assert float(ds["tp"].max()) == pytest.approx(1.0)


def test_debias_removes_a_constant_wet_bias(tmp_path, pbc_fn):
    obs = _random_obs(1).rename(precip="tp")
    # Every member is far wetter than any observation: raw says "top quintile" always.
    members = lambda v: np.full((3, 2, 2), 1000.0)  # noqa: E731
    fc = _ensemble([INIT], members)
    hc = _ensemble(_hindcast_inits(), members)

    raw = _run(pbc_fn, tmp_path, fc, hc, obs, "--components", "raw")
    np.testing.assert_allclose(raw["tp"].sel(quintile=1.0), 1.0)

    ds = _run(pbc_fn, tmp_path, fc, hc, obs, "--components", "debias")
    # Debiased toward the observed frequencies, about 1/5 per quintile.
    np.testing.assert_allclose(ds["tp"].values, 0.2, atol=0.12)


def test_persistence_learns_from_recent_weeks(tmp_path, pbc_fn):
    # One rainfall level per year, so the weeks before init tell the target week.
    rng = np.random.default_rng(2)
    level = {y: rng.gamma(2.0, 2.0, (2, 2)) for y in range(2000, 2027)}
    obs = _obs(lambda d: np.stack([level[t.year] for t in d]) + 1e-3 * rng.random((len(d), 2, 2)))
    obs = obs.rename(precip="tp")
    members = lambda v: np.full((3, 2, 2), 1000.0)  # noqa: E731  uninformative ensemble
    fc = _ensemble([INIT], members)
    hc = _ensemble(_hindcast_inits(), members)

    ds = _run(pbc_fn, tmp_path, fc, hc, obs, "--components", "persistence")

    edges = ds["quintile_threshold"].isel(time=0).values
    target = (level[2026][None] <= edges).argmax(axis=0)
    probs = ds["tp"].isel(time=0).values
    picked = np.take_along_axis(probs, target[None], axis=0)[0]
    assert (picked > 0.8).all()


def test_climatology_follows_a_recent_wet_shift(tmp_path, pbc_fn):
    rng = np.random.default_rng(3)
    # The last 10 years are all wetter than the decade before them.
    obs = _obs(
        lambda d: (
            rng.uniform(0, 1, (len(d), 2, 2)) + np.where(d.year >= 2016, 10.0, 0.0)[:, None, None]
        )
    ).rename(precip="tp")
    members = lambda v: np.full((3, 2, 2), 1.0)  # noqa: E731
    fc = _ensemble([INIT], members)
    hc = _ensemble(_hindcast_inits(), members)

    ds = _run(pbc_fn, tmp_path, fc, hc, obs, "--components", "climatology", "--clim-years", "10")

    probs = ds["tp"].isel(time=0)
    # Edges span 2006-2026, half of it wet: the dry quintiles get nothing.
    np.testing.assert_allclose(probs.sel(quintile=[0.2, 0.4]).values, 0.0, atol=0.02)
    np.testing.assert_allclose(probs.sel(quintile=[0.8, 1.0]).values, 0.4, atol=0.05)


def test_default_blends_three_components(tmp_path, pbc_fn):
    obs = _random_obs(4).rename(precip="tp")
    members = lambda v: np.full((3, 2, 2), 1000.0)  # noqa: E731
    fc = _ensemble([INIT], members)
    hc = _ensemble(_hindcast_inits(), members)

    ds = _run(pbc_fn, tmp_path, fc, hc, obs)

    assert ds["tp"].attrs["pbc_components"] == "debias,persistence,climatology"
    np.testing.assert_allclose(ds["tp"].sum("quintile"), 1.0, atol=1e-6)
    assert (ds["tp"].values >= 0).all()


def test_hindcasts_after_init_never_train(tmp_path, pbc_fn, capsys):
    obs = _random_obs(5).rename(precip="tp")
    members = lambda v: np.full((3, 2, 2), 1.0)  # noqa: E731
    fc = _ensemble([INIT], members)
    # Only hindcasts from the forecast's own season: their windows end after init.
    hc = _ensemble(pd.date_range("2026-05-25", "2026-07-15", freq="7D"), members)

    ds = _run(pbc_fn, tmp_path, fc, hc, obs, "--components", "debias")

    assert ds["tp"].isnull().all()
    assert "fewer than 10 hindcasts" in capsys.readouterr().err


def test_obs_must_match_the_forecast_window(tmp_path, pbc_fn):
    obs = _random_obs(6).rename(precip="tp")
    obs["tp"].attrs["aggregation_period"] = "1 day"
    members = lambda v: np.full((3, 2, 2), 1.0)  # noqa: E731
    with pytest.raises(SystemExit) as exc:
        _run(
            pbc_fn, tmp_path, _ensemble([INIT], members), _ensemble(_hindcast_inits(), members), obs
        )
    assert exc.value.code == 2


def test_raw_cannot_be_mixed(tmp_path, pbc_fn):
    obs = _random_obs(7).rename(precip="tp")
    members = lambda v: np.full((3, 2, 2), 1.0)  # noqa: E731
    with pytest.raises(SystemExit) as exc:
        _run(
            pbc_fn, tmp_path, _ensemble([INIT], members), _ensemble(_hindcast_inits(), members),
            obs, "--components", "raw,debias",
        )  # fmt: skip
    assert exc.value.code == 2


def test_members_are_compared_in_obs_units(tmp_path, pbc_fn):
    obs = _random_obs(8)
    members = lambda v: np.repeat(_obs_at(obs, v)[None], 3, axis=0) / 24.0  # noqa: E731
    fc, hc = _ensemble([INIT], members), _ensemble(_hindcast_inits(), members)
    for ds in (fc, hc):
        ds["tp"].attrs["units"] = "mm hr-1"

    ds = _run(pbc_fn, tmp_path, fc, hc, obs.rename(precip="tp"), "--components", "raw")

    edges = ds["quintile_threshold"].isel(time=0).values
    y = _obs_at(obs, INIT + pd.Timedelta(days=STEP))
    expected = (y[None] <= edges).argmax(axis=0)
    np.testing.assert_array_equal(ds["tp"].isel(time=0).argmax("quintile").values, expected)
    assert ds["quintile_threshold"].attrs["units"] == "mm day-1"


def test_cells_without_observed_history_stay_nan(tmp_path, pbc_fn):
    obs = _random_obs(9).rename(precip="tp")
    obs["tp"][:, 0, 0] = np.nan  # one cell never observed
    members = lambda v: np.full((3, 2, 2), 1.0)  # noqa: E731
    fc, hc = _ensemble([INIT], members), _ensemble(_hindcast_inits(), members)

    for components in ("raw", "debias,persistence,climatology"):
        ds = _run(pbc_fn, tmp_path, fc, hc, obs, "--components", components)
        probs = ds["tp"].isel(time=0)
        assert probs.isel(latitude=0, longitude=0).isnull().all()
        assert probs.isel(latitude=1, longitude=1).notnull().all()


def test_incomplete_obs_windows_are_missing(tmp_path, pbc_fn):
    obs = _random_obs(10).rename(precip="tp")
    # Every window before 2026 is incomplete: nothing to train on.
    coverage = np.where(obs["time"].dt.year < 2026, 6 / 7, 1.0)
    obs = obs.assign_coords(aggregation_coverage=("time", coverage))
    members = lambda v: np.full((3, 2, 2), 1.0)  # noqa: E731
    fc, hc = _ensemble([INIT], members), _ensemble(_hindcast_inits(), members)
    ds = _run(pbc_fn, tmp_path, fc, hc, obs, "--components", "debias")
    assert ds["tp"].isnull().all()

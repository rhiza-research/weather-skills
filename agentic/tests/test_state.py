"""Canonical state must ignore representation and track semantics."""

import numpy as np
from conftest import make_forecast, make_gridded, write_zarr

from agentic.state import artifact_state, canonical_state, state_hash, to_json

OBJ = {
    "intent": "map",
    "quantity": "precipitation",
    "variables": ["precip"],
    "region": None,
    "temporal_scale": "weekly",
    "output": "figure",
}


def _two_var(order):
    ds = make_gridded(n_time=3)
    ds["t2m"] = ds["precip"] * 0 + 290.0
    ds["t2m"].attrs.update(units="K", standard_name="air_temperature")
    return ds[list(order)]


def test_lat_lon_naming_is_irrelevant():
    a = make_gridded(n_time=3)
    b = a.rename({"latitude": "lat", "longitude": "lon"})
    assert to_json(artifact_state(a, "observation")) == to_json(artifact_state(b, "observation"))


def test_dimension_order_is_irrelevant():
    a = make_gridded(n_time=3)
    b = a.transpose("longitude", "latitude", "time")
    assert artifact_state(a, "observation") == artifact_state(b, "observation")


def test_variable_order_is_irrelevant():
    assert artifact_state(_two_var(["precip", "t2m"]), "o") == artifact_state(
        _two_var(["t2m", "precip"]), "o"
    )


def test_chunking_and_path_are_irrelevant(tmp_path):
    import xarray as xr

    ds = make_gridded(n_time=4)
    p1, p2 = tmp_path / "a" / "x.zarr", tmp_path / "b" / "y.zarr"
    write_zarr(ds, p1)
    # different on-disk chunk layout (no dask needed: Zarr encoding)
    ds.to_zarr(p2, mode="w", consolidated=True, encoding={"precip": {"chunks": (1, 3, 4)}})
    s1 = artifact_state(xr.open_zarr(p1), "observation")
    s2 = artifact_state(xr.open_zarr(p2), "observation")
    assert s1 == s2
    assert str(tmp_path) not in to_json(s1)


def test_member_and_step_aliases_normalise():
    ds = make_forecast(n_step=3, members=2)
    s = artifact_state(ds, "forecast")
    assert s["dims"][:3] == ["member", "init_time", "prediction_timedelta"]
    assert s["time_representation"] == "init+lead"
    assert s["ensemble"]["present"] is True
    assert s["dataset_type"] == "ensemble_forecast"


def test_semantics_change_the_state():
    rate = make_gridded(n_time=3)
    amount = rate.copy()
    amount["precip"].attrs.update(units="mm", standard_name="lwe_thickness_of_precipitation_amount")
    assert state_hash(canonical_state(OBJ, {"obs": rate})) != state_hash(
        canonical_state(OBJ, {"obs": amount})
    )
    assert artifact_state(rate, "o")["variables"]["precip"]["units_kind"] == "precip"
    assert artifact_state(amount, "o")["variables"]["precip"]["units_kind"] == "precip_amount"


def test_state_never_reads_values():
    ds = make_gridded(n_time=3)
    ds["precip"].values[:] = np.nan  # values must not influence the state
    assert artifact_state(ds, "o") == artifact_state(make_gridded(n_time=3), "o")


def test_history_reads_real_skill_provenance(tmp_path):
    import xarray as xr
    from conftest import load_skill, run_skill

    agg = load_skill("aggregate-temporal", "aggregate").aggregate
    src = write_zarr(make_gridded(n_time=14), tmp_path / "in.zarr")
    out = tmp_path / "weekly.zarr"
    run_skill(agg, "-i", str(src), "-o", str(out), "--period", "weekly")
    s = artifact_state(xr.open_zarr(out), "observation")
    assert s["history"]["recent_skills"][-1] == "aggregate-temporal"
    assert s["history"]["intact"] is True
    assert s["variables"]["precip"]["aggregation_period"] == "7 day"

"""Routing events: opt-in, schema-checked, path-free, distinctions explicit."""

import json

import pytest
from conftest import make_gridded

from agentic.catalog import catalog_fingerprint, load_actions
from agentic.events import ENV_VAR, RoutingLogger, invalid_from_verdicts
from agentic.predicates import eligible_actions
from agentic.state import artifact_state, canonical_state


def _state():
    return canonical_state(
        {"intent": "map", "temporal_scale": "weekly", "region": None},
        {"obs": artifact_state(make_gridded(n_time=8), "observation")},
    )


def test_logger_is_off_by_default(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert (
        RoutingLogger().log(
            state_before={}, eligible_actions=["x"], selected_action="x", selection_source="rule"
        )
        is None
    )


def test_logged_event_round_trips(tmp_path):
    s = _state()
    elig, verdicts = eligible_actions(s, load_actions())
    assert "aggregate-temporal" in elig and "convert-to-totals" not in elig
    log = RoutingLogger(path=tmp_path / "r.jsonl", workflow_id="w1")
    ev = log.log(
        state_before=s,
        eligible_actions=elig,
        selected_action="aggregate-temporal",
        selection_source="reference",
        scientific_parameters={"period": "weekly"},
        invalid_actions=invalid_from_verdicts(verdicts),
        verification={"units": True},
    )
    row = json.loads((tmp_path / "r.jsonl").read_text().strip())
    assert row["outcome"] == "positive" and row["step_index"] == 0
    assert row["action_catalog_hash"] == catalog_fingerprint()
    assert "NOT_AGGREGATED" in row["invalid_actions"]["convert-to-totals"]
    assert str(tmp_path) not in json.dumps(row["state_before"])
    assert ev.outcome == "positive"


def test_ineligible_selection_is_rejected_unless_human(tmp_path):
    log = RoutingLogger(path=tmp_path / "r.jsonl")
    with pytest.raises(ValueError):
        log.log(
            state_before={}, eligible_actions=["a"], selected_action="b", selection_source="clm"
        )
    assert log.log(
        state_before={}, eligible_actions=["a"], selected_action="b", selection_source="human"
    )


@pytest.mark.parametrize(
    "value", ["/tmp/out.zarr", r"C:\data\x", "fc.zarr", "./a", "~/x", "b.GeoJSON"]
)
def test_paths_in_scientific_parameters_are_refused(tmp_path, value):
    log = RoutingLogger(path=tmp_path / "r.jsonl")
    with pytest.raises(ValueError):
        log.log(state_before={}, eligible_actions=["a"], selected_action="a",
                selection_source="rule", scientific_parameters={"x": value})  # fmt: skip


def test_scientific_values_are_kept(tmp_path):
    log = RoutingLogger(path=tmp_path / "r.jsonl")
    params = {"period": "weekly", "t": 50.0, "u": "mm/day"}
    ev = log.log(state_before={}, eligible_actions=["a"], selected_action="a",
                 selection_source="rule", scientific_parameters=params)  # fmt: skip
    assert ev.scientific_parameters == {"period": "weekly", "t": 50.0, "u": "mm/day"}

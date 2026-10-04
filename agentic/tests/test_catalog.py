"""Contracts are complete, consistent with the skills tree, and fingerprinted."""

import json
from pathlib import Path

from agentic.catalog import (
    ACTIONS_PATH,
    PROPOSED_OR_EXTERNAL,
    catalog_fingerprint,
    confusion_map,
    load_actions,
)
from agentic.predicates import PAIRWISE, UNARY

SKILLS = {p.name for p in (Path(__file__).resolve().parents[2] / "skills").iterdir() if p.is_dir()}
FIELDS = {"action_semantic_version", "action_class", "short_description", "nearest_confusions"}


def test_every_contract_has_every_field():
    for name, c in load_actions().items():
        assert FIELDS <= set(c), f"{name} missing {sorted(FIELDS - set(c))}"


def test_contracts_name_real_skills():
    assert set(load_actions()) <= SKILLS


def test_descriptions_are_short_and_contrastive():
    for name, c in load_actions().items():
        assert len(c["short_description"]) <= 160, name
        assert c["nearest_confusions"], f"{name} names no nearest confusion"
        assert name not in c["nearest_confusions"]


def test_confusions_resolve():
    for name, others in confusion_map().items():
        for o in others:
            assert o in SKILLS or o in PROPOSED_OR_EXTERNAL, f"{name} -> unknown {o!r}"


def test_every_predicate_has_a_contract():
    assert (set(UNARY) | set(PAIRWISE)) <= set(load_actions())


def test_fingerprint_is_deterministic_and_sensitive(tmp_path):
    assert catalog_fingerprint() == catalog_fingerprint()
    doc = json.loads(ACTIONS_PATH.read_text(encoding="utf-8"))
    doc["actions"]["coarsen"]["short_description"] += " (edited)"
    alt = tmp_path / "actions.json"
    alt.write_text(json.dumps(doc), encoding="utf-8")
    assert catalog_fingerprint(actions_path=alt) != catalog_fingerprint()

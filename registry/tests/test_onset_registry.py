"""Registry schema, content hash, and compilation against the real indicator grammar."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from registry import onset

REPO = Path(__file__).resolve().parents[2]
INDICATOR_SPEC = REPO / "skills" / "indicator" / "spec.py"


@pytest.fixture(scope="module")
def defs() -> dict[str, dict]:
    return onset.load()


@pytest.fixture(scope="module")
def indicator_spec():
    """The indicator skill's rule parser, loaded by path (skills are not packages)."""
    if not INDICATOR_SPEC.is_file():
        pytest.skip("indicator skill is not on this branch")
    name = "_registry_test_indicator_spec"
    spec = importlib.util.spec_from_file_location(name, INDICATOR_SPEC)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


def _validate(name: str, d: dict, defs: dict) -> None:
    all_defs = {**defs, name: d}
    onset.validate(name, d, all_defs)


# ---------------------------------------------------------------- schema: positive


def test_registry_loads_and_every_entry_validates(defs):
    assert {"icpac-onset", "agrhymet-sos", "moron-robertson-2014"} <= set(defs)
    for name, d in defs.items():
        onset.validate(name, d, defs)


def test_variants_name_a_registered_parent(defs):
    for d in defs.values():
        if d["status"] != "canonical":
            assert d["derived_from"] in defs


# ---------------------------------------------------------------- schema: negative


def test_bad_status_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["status"] = "official"
    with pytest.raises(onset.RegistryError, match="status"):
        _validate("x", d, defs)


def test_missing_source_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    del d["source"]
    with pytest.raises(onset.RegistryError, match="source"):
        _validate("x", d, defs)


def test_unspecified_in_source_naming_missing_field_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["unspecified_in_source"] = ["trigger.no_such_field"]
    with pytest.raises(onset.RegistryError, match="unspecified_in_source"):
        _validate("x", d, defs)


def test_variant_without_registered_parent_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset-north"])
    d["derived_from"] = "nope"
    with pytest.raises(onset.RegistryError, match="derived_from"):
        _validate("x", d, defs)


# ---------------------------------------------------------------- tunables


def test_every_non_candidate_declares_tunables_with_a_why(defs):
    for name, d in defs.items():
        if d["status"] != "candidate":
            assert d["tunable"]["why"].strip(), name


def test_agrhymet_thresholds_and_time_basis_are_fixed(defs):
    for name in ("agrhymet-sos", "agrhymet-sos-rolling"):
        fixed = set(onset.fixed_fields(defs[name]))
        assert {"time_basis", "trigger.total_mm", "confirm.total_mm"} <= fixed, name
        assert not onset.tunable_fields(defs[name]), name


def test_icpac_thresholds_are_tunable(defs):
    tun = onset.tunable_fields(defs["icpac-onset"])
    assert {"trigger.total_mm", "trigger.window_days", "veto.follow_days"} <= set(tun)


def test_missing_tunable_table_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    del d["tunable"]
    with pytest.raises(onset.RegistryError, match="tunable"):
        _validate("x", d, defs)


def test_tunable_naming_missing_field_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["tunable"]["trigger.no_such_field"] = {"min": 1.0, "max": 2.0}
    with pytest.raises(onset.RegistryError, match="missing parameter field"):
        _validate("x", d, defs)


def test_tunable_on_prose_field_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["tunable"]["name"] = {"choices": [d["name"]]}
    with pytest.raises(onset.RegistryError, match="missing parameter field"):
        _validate("x", d, defs)


def test_fixed_naming_missing_field_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["tunable"]["fixed"] = [*d["tunable"]["fixed"], "confirm.total_mm"]
    with pytest.raises(onset.RegistryError, match="fixed names missing"):
        _validate("x", d, defs)


def test_field_both_tunable_and_fixed_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["tunable"]["fixed"] = [*d["tunable"]["fixed"], "trigger.total_mm"]
    with pytest.raises(onset.RegistryError, match="both tunable and fixed"):
        _validate("x", d, defs)


def test_current_value_outside_bounds_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["tunable"]["trigger.total_mm"] = {"min": 25.0, "max": 40.0}
    with pytest.raises(onset.RegistryError, match="outside"):
        _validate("x", d, defs)


def test_current_value_not_in_choices_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["tunable"]["veto.follow_days"] = {"choices": [30, 45]}
    with pytest.raises(onset.RegistryError, match="not among the choices"):
        _validate("x", d, defs)


def test_float_bounds_on_int_field_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["tunable"]["trigger.window_days"] = {"min": 3.0, "max": 7.0}
    with pytest.raises(onset.RegistryError, match="field's type"):
        _validate("x", d, defs)


def test_tunable_without_why_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    del d["tunable"]["why"]
    with pytest.raises(onset.RegistryError, match="why"):
        _validate("x", d, defs)


# ---------------------------------------------------------------- candidates


def _candidate(defs, parent="icpac-onset", **changes):
    """A well-formed candidate derived from `parent`, with dotted-field `changes` applied."""
    p = defs[parent]
    d = {
        "status": "candidate",
        "derived_from": parent,
        "why": "test candidate",
        "name": "test",
        "source": {"citation": "test optimisation", "url": "https://example.org"},
        **{k: copy.deepcopy(p[k]) for k in onset.PARAMETER_SECTIONS if k in p},
        "optimization": {
            "objective": "maximise onset-date skill",
            "data": "CHIRPS v2.0, 1991-2020",
            "method": "grid search",
            "validation": "leave-one-year-out",
            "date": "2026-09-27",
            "parent_hash": onset.content_hash(p),
            "distance_from_parent": 0.25,
        },
    }
    for dotted, value in changes.items():
        *path, leaf = dotted.replace("__", ".").split(".")
        node = d
        for part in path:
            node = node[part]
        node[leaf] = value
    return d


def test_well_formed_candidate_validates(defs):
    cand = _candidate(defs, **{"trigger__total_mm": 25.0, "veto__follow_days": 30})
    _validate("cand", cand, defs)
    changed = onset.validate_candidate_against_parent(cand, defs["icpac-onset"])
    assert changed == ["trigger.total_mm", "veto.follow_days"]


def test_candidate_missing_optimization_rejected(defs):
    cand = _candidate(defs)
    del cand["optimization"]
    with pytest.raises(onset.RegistryError, match="optimization"):
        _validate("cand", cand, defs)


def test_candidate_missing_parent_hash_rejected(defs):
    cand = _candidate(defs)
    del cand["optimization"]["parent_hash"]
    with pytest.raises(onset.RegistryError, match="parent_hash"):
        _validate("cand", cand, defs)


def test_candidate_non_numeric_distance_rejected(defs):
    cand = _candidate(defs)
    cand["optimization"]["distance_from_parent"] = "small"
    with pytest.raises(onset.RegistryError, match="distance_from_parent"):
        _validate("cand", cand, defs)


def test_candidate_without_derived_from_rejected(defs):
    cand = _candidate(defs)
    del cand["derived_from"]
    with pytest.raises(onset.RegistryError, match="derived_from"):
        _validate("cand", cand, defs)


def test_candidate_changing_fixed_field_rejected(defs):
    cand = _candidate(defs, parent="agrhymet-sos", **{"trigger__total_mm": 20.0})
    _validate("cand", cand, defs)  # well-formed on its own ...
    with pytest.raises(onset.RegistryError, match="fixed"):  # ... but moves a fixed field
        onset.validate_candidate_against_parent(cand, defs["agrhymet-sos"])


def test_candidate_changing_untunable_field_rejected(defs):
    cand = _candidate(defs, **{"veto__follow_anchor": "run_start_after_trigger_end"})
    with pytest.raises(onset.RegistryError, match="not tunable"):
        onset.validate_candidate_against_parent(cand, defs["icpac-onset"])


def test_candidate_outside_parent_bounds_rejected(defs):
    cand = _candidate(defs, **{"trigger__total_mm": 55.0})
    with pytest.raises(onset.RegistryError, match="outside"):
        onset.validate_candidate_against_parent(cand, defs["icpac-onset"])


def test_candidate_int_field_given_float_rejected(defs):
    cand = _candidate(defs, **{"trigger__window_days": 5.0})
    with pytest.raises(onset.RegistryError, match="field's type"):
        onset.validate_candidate_against_parent(cand, defs["icpac-onset"])


def test_candidate_with_stale_parent_hash_rejected(defs):
    cand = _candidate(defs)
    cand["optimization"]["parent_hash"] = "000000000000"
    with pytest.raises(onset.RegistryError, match="stale"):
        onset.validate_candidate_against_parent(cand, defs["icpac-onset"])


def test_load_checks_candidates_against_their_parent(defs, tmp_path):
    import tomllib

    text = onset.REGISTRY_PATH.read_text(encoding="utf-8")
    good = onset.content_hash(defs["icpac-onset"])
    block = f"""
[definitions.icpac-onset-test-candidate]
status = "candidate"
derived_from = "icpac-onset"
why = "test"
name = "test"
source.citation = "test"
source.url = "https://example.org"
time_basis = "rolling_daily"
trigger.window_days = 3
trigger.total_mm = 60.0
trigger.total_op = ">="
veto.mode = "consecutive_dry"
veto.dry_days = 7
veto.dry_day_mm = 1.0
veto.follow_days = 21
veto.follow_anchor = "run_start_after_trigger_start"
search.start = ["02-01", "08-01"]
search.window_days = 60

[definitions.icpac-onset-test-candidate.optimization]
objective = "o"
data = "d"
method = "m"
validation = "v"
date = "2026-09-27"
parent_hash = "{good}"
distance_from_parent = 1.0
"""
    path = tmp_path / "onset_definitions.toml"
    path.write_text(text + block, encoding="utf-8")
    assert (
        "icpac-onset-test-candidate"
        in tomllib.loads(path.read_text(encoding="utf-8"))["definitions"]
    )
    with pytest.raises(onset.RegistryError, match="icpac-onset-test-candidate.*outside"):
        onset.load(path)
    path.write_text(text + block.replace("60.0", "30.0"), encoding="utf-8")
    assert "icpac-onset-test-candidate" in onset.load(path)


# ---------------------------------------------------------------- content hash


def test_content_hash_is_stable_under_prose_edits(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    before = onset.content_hash(d)
    d["name"] = "renamed"
    d["notes"] = "reworded"
    d["source"]["checked"] = "2099-01-01"
    d["unspecified_in_source"] = []
    assert onset.content_hash(d) == before


def test_content_hash_is_sensitive_to_parameter_edits(defs):
    base = defs["icpac-onset"]
    before = onset.content_hash(base)
    for dotted, value in [
        ("trigger.total_mm", 25.0),
        ("trigger.total_op", ">"),
        ("veto.follow_days", 30),
        ("search.window_days", 45),
        ("time_basis", "calendar_dekad"),
    ]:
        d = copy.deepcopy(base)
        *path, leaf = dotted.split(".")
        node = d
        for part in path:
            node = node[part]
        node[leaf] = value
        assert onset.content_hash(d) != before, dotted


def test_content_hash_recipe_is_the_documented_one(defs):
    # The contract skills reproduce without importing registry/. Keep this literal.
    import hashlib
    import json

    d = defs["agrhymet-sos"]
    keep = {k: d[k] for k in ("time_basis", "trigger", "confirm", "veto", "search") if k in d}
    expected = hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]
    assert onset.content_hash(d) == expected
    assert len(expected) == 12


def test_content_hashes_are_distinct(defs):
    hashes = [onset.content_hash(d) for d in defs.values()]
    assert len(set(hashes)) == len(hashes)


# ---------------------------------------------------------------- compilation


def _compile(name, d):
    per_cell = d["trigger"].get("threshold_kind") == "per_cell_climatology"
    return onset.compile_to_indicator(name, d, scalar_threshold=10.0 if per_cell else None)


def test_every_definition_compiles_and_parses_with_indicator_grammar(defs, indicator_spec):
    for name, d in defs.items():
        compiled = _compile(name, d)
        parsed = indicator_spec.parse_rule(compiled.rule)
        assert parsed.clauses, name
        assert compiled.exact == (not compiled.dropped)


def test_per_cell_definition_requires_scalar_threshold(defs):
    with pytest.raises(onset.RegistryError, match="per-cell"):
        onset.compile_to_indicator("moron-robertson-2014", defs["moron-robertson-2014"])


def test_calendar_dekad_is_reported_as_dropped(defs):
    compiled = _compile("agrhymet-sos", defs["agrhymet-sos"])
    assert not compiled.exact
    assert any("dekad" in reason for reason in compiled.dropped)


# ---------------------------------------------------------------- parity with indicator
# indicator reads the registry itself (skills/indicator/references copy) and carries its own
# copy of the rule compiler, because a self-contained skill cannot import registry/. These
# tests keep the two compilers identical, so drift is caught here instead of in outputs.


def _compile_or_none(name, d):
    try:
        return onset.compile_to_indicator(name, d)
    except onset.RegistryError:
        return None


def test_indicator_compiler_matches_registry_compiler(defs, indicator_spec):
    for name, d in defs.items():
        ours = _compile_or_none(name, d)
        rule, dropped = indicator_spec.compile_definition(d)
        if ours is None:
            assert rule is None, f"{name}: registry refuses, indicator compiled {rule!r}"
            continue
        assert rule == ours.rule, name
        assert list(dropped) == list(ours.dropped), name


def test_legacy_aliases_resolve_to_registry_rules(defs, indicator_spec):
    for alias, registry_id in indicator_spec.LEGACY_ALIASES.items():
        assert registry_id in defs, f"{alias} -> unknown registry id {registry_id!r}"
        expected = onset.compile_to_indicator(registry_id, defs[registry_id]).rule
        assert indicator_spec.parse_rule(alias).expanded == expected, alias


def test_legacy_aliases_expand_exactly_as_before_the_registry(indicator_spec):
    """Resolving through the registry must not change what the old aliases mean."""
    before = {
        "icpac-onset": "precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d",
        "chc-onset": "precip sum 10d > 25 and precip sum 20d > 20 after 10d",
    }
    for alias, rule in before.items():
        assert indicator_spec.parse_rule(alias).expanded == rule, alias


# ---------------------------------------------------------------- sync tool


@pytest.fixture(scope="module")
def sync_tool():
    name = "_registry_test_sync_definitions"
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / "sync_definitions.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def fake_repo(tmp_path):
    """A throwaway repo: canonical file + skills/alpha and skills/beta (never the real skills)."""
    canonical = tmp_path / "registry" / "onset_definitions.toml"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"schema_version = 1\n# line two\n")
    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "beta").mkdir(parents=True)
    return canonical, skills


def test_sync_consumers_include_indicator_and_onset_date(sync_tool):
    assert {"indicator", "onset-date"} <= set(sync_tool.CONSUMERS)
    assert sync_tool.CANONICAL == onset.REGISTRY_PATH


def test_sync_writes_lf_copies_and_check_passes(sync_tool, fake_repo):
    canonical, skills = fake_repo
    canonical.write_bytes(b"schema_version = 1\r\n# line two\r\n")
    consumers = ("alpha", "beta", "gamma")  # gamma does not exist on this "branch"
    lines = sync_tool.sync(canonical, skills, consumers)
    assert any("skip gamma" in line for line in lines)
    for skill in ("alpha", "beta"):
        copy_ = skills / skill / "references" / "onset_definitions.toml"
        assert copy_.read_bytes() == b"schema_version = 1\n# line two\n"
    assert not (skills / "gamma").exists()
    problems, notes = sync_tool.check(canonical, skills, consumers)
    assert problems == []
    assert any("skip gamma" in n for n in notes)


def test_check_detects_missing_copy(sync_tool, fake_repo):
    canonical, skills = fake_repo
    sync_tool.sync(canonical, skills, ("alpha",))
    problems, _ = sync_tool.check(canonical, skills, ("alpha", "beta"))
    assert problems == ["skills/beta/references/onset_definitions.toml: missing"]


def test_check_detects_differing_copy(sync_tool, fake_repo):
    canonical, skills = fake_repo
    sync_tool.sync(canonical, skills, ("alpha", "beta"))
    (skills / "alpha" / "references" / "onset_definitions.toml").write_bytes(b"edited\n")
    problems, _ = sync_tool.check(canonical, skills, ("alpha", "beta"))
    assert len(problems) == 1 and "skills/alpha/" in problems[0] and "differs" in problems[0]


def test_check_ignores_crlf_only_difference(sync_tool, fake_repo):
    canonical, skills = fake_repo
    sync_tool.sync(canonical, skills, ("alpha",))
    (skills / "alpha" / "references" / "onset_definitions.toml").write_bytes(
        b"schema_version = 1\r\n# line two\r\n"
    )
    assert sync_tool.check(canonical, skills, ("alpha",)) == ([], [])


def test_check_main_exits_1_on_drift(sync_tool, fake_repo, monkeypatch, capsys):
    canonical, skills = fake_repo
    monkeypatch.setattr(sync_tool, "CANONICAL", canonical)
    monkeypatch.setattr(sync_tool, "SKILLS_DIR", skills)
    monkeypatch.setattr(sync_tool, "CONSUMERS", ("alpha",))
    assert sync_tool.main(["--check"]) == 1
    assert "skills/alpha/references/onset_definitions.toml: missing" in capsys.readouterr().out
    assert sync_tool.main(["--bogus"]) == 2


def test_sheerwater_spw_onset_compiles_exactly(defs, indicator_spec):
    """Rhiza's own onset condition: (11-day > 40 mm) & (8-day > 30 mm), same start day."""
    c = onset.compile_to_indicator("sheerwater-spw-rainy-onset", defs["sheerwater-spw-rainy-onset"])
    assert c.exact
    assert c.rule == "precip sum 8d > 30 and precip sum 11d > 40"
    assert indicator_spec.parse_rule("sheerwater-spw-rainy-onset").expanded == c.rule


# ---------------------------------------------------------------- Sheerwater's definitions


@pytest.mark.parametrize(
    ("name", "rule"),
    [
        ("sheerwater-spw-rainy-onset", "precip sum 8d > 30 and precip sum 11d > 40"),
        ("sheerwater-icpac-onset", "precip sum 3d > 21 and precip sum 7d > 10.5 after 3d"),
        ("sheerwater-chc-onset", "precip sum 10d > 25 and precip sum 20d > 20 after 10d"),
        ("sheerwater-moron-robertson-onset", "precip sum 5d > 38 and precip sum 10d > 5 after 5d"),
    ],
)
def test_sheerwater_definitions_compile_exactly(defs, name, rule):
    """Rhiza's own events (sheerwater/interfaces/events.py, tasks/spw.py): strict '>' sums;
    later windows start where the trigger window ends, except the planting window (same day)."""
    c = onset.compile_to_indicator(name, defs[name])
    assert c.exact, c.dropped
    assert c.rule == rule

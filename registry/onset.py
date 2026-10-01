"""Load and validate the onset-definition registry; compile entries to indicator rules.

A definition is data. Consumers (indicator aliases, onset-date defaults, an agent choosing a
definition as a scientific parameter) read it from here instead of restating it. Each entry
has a content hash, so a routing log or output provenance can record exactly which version
was used.

compile_to_indicator() translates an entry into the indicator skill's --rule grammar and
reports, honestly, what the grammar cannot express (per-cell thresholds, search start dates,
calendar dekads, inclusive wet-day tests). A compiled rule is only `exact` when nothing was
dropped.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent / "onset_definitions.toml"
STATUSES = {"canonical", "variant", "candidate"}
OPS = {">", ">=", "<", "<="}
VETO_MODES = {"none", "consecutive_dry", "window_sum"}
TIME_BASES = {"rolling_daily", "calendar_dekad"}
OPTIMIZATION_FIELDS = {
    "objective",
    "data",
    "method",
    "validation",
    "date",
    "parent_hash",
    "distance_from_parent",
}
# The scientific sections: the ones content_hash() covers, and the only ones a tunable or
# fixed entry may name.
PARAMETER_SECTIONS = ("time_basis", "trigger", "confirm", "veto", "search")


class RegistryError(ValueError):
    pass


def load(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    with path.open("rb") as f:
        doc = tomllib.load(f)
    defs = doc.get("definitions", {})
    for name, d in defs.items():
        validate(name, d, defs)
    for name, d in defs.items():
        if d["status"] == "candidate":
            try:
                validate_candidate_against_parent(d, defs[d["derived_from"]])
            except RegistryError as exc:
                raise RegistryError(f"{name}: {exc}") from None
    return defs


def validate(name: str, d: dict, all_defs: dict) -> None:
    def need(cond, msg):
        if not cond:
            raise RegistryError(f"{name}: {msg}")

    need(d.get("status") in STATUSES, f"status must be one of {sorted(STATUSES)}")
    need(
        d.get("source", {}).get("citation") and d["source"].get("url"), "needs source citation+url"
    )
    need(d.get("time_basis") in TIME_BASES, f"time_basis must be one of {sorted(TIME_BASES)}")
    t = d.get("trigger", {})
    need(isinstance(t.get("window_days"), int) and t["window_days"] >= 1, "trigger.window_days")
    need(t.get("total_op") in OPS, "trigger.total_op")
    per_cell = t.get("threshold_kind") == "per_cell_climatology"
    need(per_cell or isinstance(t.get("total_mm"), float), "trigger.total_mm or per-cell threshold")
    if t.get("all_days_wet"):
        need(t.get("wet_day_op") in (">", ">="), "trigger.wet_day_op for all_days_wet")
    v = d.get("veto", {})
    need(v.get("mode") in VETO_MODES, f"veto.mode must be one of {sorted(VETO_MODES)}")
    if v["mode"] != "none":
        need(isinstance(v.get("follow_days"), int) and v["follow_days"] >= 1, "veto.follow_days")
    if d["status"] in ("variant", "candidate"):
        parent = d.get("derived_from")
        need(parent in all_defs, f"derived_from {parent!r} must name a registered definition")
        need(d.get("why"), "variant/candidate needs a 'why'")
    if d["status"] == "candidate":
        opt = d.get("optimization", {})
        missing = sorted(OPTIMIZATION_FIELDS - set(opt))
        need(not missing, f"candidate needs optimization.{missing}")
        need(
            isinstance(opt.get("parent_hash"), str) and len(opt["parent_hash"]) == 12,
            "optimization.parent_hash must be the parent's 12-character content hash",
        )
        dist = opt.get("distance_from_parent")
        need(
            isinstance(dist, int | float) and not isinstance(dist, bool) and dist >= 0,
            "optimization.distance_from_parent must be a non-negative number",
        )
    for key in d.get("unspecified_in_source", []):
        need(has_field(d, key), f"unspecified_in_source names missing field {key!r}")
    if d["status"] != "candidate" or "tunable" in d:
        need("tunable" in d, "needs a [tunable] table (it may list only 'fixed' and 'why')")
        for problem in _tunable_problems(d):
            need(False, problem)


def _is_number(x) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def _tunable_problems(d: dict) -> list[str]:
    """Everything wrong with d's [tunable] table (empty list when it is sound)."""
    tun = d.get("tunable")
    if not isinstance(tun, dict):
        return ["tunable must be a table"]
    problems = []
    if not isinstance(tun.get("why"), str) or not tun["why"].strip():
        problems.append("tunable.why must justify the bounds in one line")
    fixed = tun.get("fixed", [])
    if not isinstance(fixed, list) or not all(isinstance(f, str) for f in fixed):
        return [*problems, "tunable.fixed must be a list of dotted field names"]
    for key in fixed:
        if key.split(".")[0] not in PARAMETER_SECTIONS or not has_field(d, key):
            problems.append(f"tunable.fixed names missing parameter field {key!r}")
    for key, bounds in tunable_fields(d).items():
        if key.split(".")[0] not in PARAMETER_SECTIONS or not has_field(d, key):
            problems.append(f"tunable names missing parameter field {key!r}")
            continue
        if key in fixed:
            problems.append(f"{key!r} cannot be both tunable and fixed")
        value = get_field(d, key)
        problems.extend(f"tunable {key!r}: {p}" for p in _bounds_problems(value, bounds))
    return problems


def _bounds_problems(value, bounds) -> list[str]:
    if not isinstance(bounds, dict):
        return ["bounds must be a table with min+max or choices"]
    if set(bounds) == {"choices"}:
        choices = bounds["choices"]
        if not isinstance(choices, list) or not choices:
            return ["choices must be a non-empty list"]
        if any(type(c) is not type(value) for c in choices):
            return [f"choices must all have the field's type ({type(value).__name__})"]
        return [] if value in choices else [f"current value {value!r} is not among the choices"]
    if set(bounds) == {"min", "max"}:
        lo, hi = bounds["min"], bounds["max"]
        if not _is_number(value):
            return [f"min/max bounds need a numeric field, got {value!r}; use choices"]
        if type(lo) is not type(value) or type(hi) is not type(value):
            return [f"min/max must have the field's type ({type(value).__name__})"]
        if lo > hi:
            return ["min exceeds max"]
        return [] if lo <= value <= hi else [f"current value {value!r} is outside [{lo}, {hi}]"]
    return ["bounds must be exactly {min, max} or {choices}"]


def tunable_fields(d: dict) -> dict[str, dict]:
    """Dotted field name -> bounds, for the fields a candidate may move (excludes fixed/why)."""
    return {k: v for k, v in d.get("tunable", {}).items() if k not in ("fixed", "why")}


def fixed_fields(d: dict) -> list[str]:
    return list(d.get("tunable", {}).get("fixed", []))


def _flatten(d: dict) -> dict[str, object]:
    """Dotted name -> value for every leaf of the parameter sections."""
    out: dict[str, object] = {}

    def walk(prefix: str, node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        else:
            out[prefix] = node

    for section in PARAMETER_SECTIONS:
        if section in d:
            walk(section, d[section])
    return out


def validate_candidate_against_parent(candidate: dict, parent: dict) -> list[str]:
    """Check a candidate only moves fields its parent declares tunable, within their bounds.

    Also checks optimization.parent_hash still equals the parent's content hash: a parent whose
    parameters changed after the optimisation makes the candidate stale. Returns the sorted
    list of changed dotted fields; raises RegistryError on any violation.
    """
    problems = []
    recorded = candidate.get("optimization", {}).get("parent_hash")
    if recorded != content_hash(parent):
        problems.append(
            f"optimization.parent_hash {recorded!r} != parent's content hash "
            f"{content_hash(parent)!r} (candidate is stale or mis-attributed)"
        )
    mine, theirs = _flatten(candidate), _flatten(parent)
    changed = sorted(k for k in mine.keys() | theirs.keys() if mine.get(k) != theirs.get(k))
    tunable, fixed = tunable_fields(parent), fixed_fields(parent)
    for key in changed:
        if key in fixed:
            problems.append(f"{key!r} is fixed in the parent and must not change")
        elif key not in tunable:
            problems.append(f"{key!r} changed but is not tunable in the parent")
        elif key not in mine:
            problems.append(f"{key!r} is tunable in the parent but missing from the candidate")
        else:
            problems.extend(f"{key!r}: {p}" for p in _bounds_problems(mine[key], tunable[key]))
    if problems:
        raise RegistryError("; ".join(problems))
    return changed


_MISSING = object()


def get_field(d: dict, dotted: str, default=_MISSING):
    """Return d["a"]["b"] for dotted name "a.b"; raise KeyError unless a default is given."""
    node = d
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is _MISSING:
                raise KeyError(dotted)
            return default
        node = node[part]
    return node


def has_field(d: dict, dotted: str) -> bool:
    absent = object()
    return get_field(d, dotted, absent) is not absent


def content_hash(d: dict) -> str:
    """Hash of the scientific content only (not prose), so wording edits keep the identity."""
    keep = {k: d[k] for k in ("time_basis", "trigger", "confirm", "veto", "search") if k in d}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


@dataclass
class Compiled:
    name: str
    rule: str
    exact: bool
    dropped: list[str] = field(default_factory=list)


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def compile_to_indicator(
    name: str, d: dict, variable: str = "precip", scalar_threshold: float | None = None
) -> Compiled:
    """Translate one definition into the indicator --rule grammar."""
    dropped: list[str] = []
    t, v = d["trigger"], d["veto"]
    w = t["window_days"]
    clauses = []
    if t.get("all_days_wet"):
        # indicator counts a wet day as value > daily threshold (strict)
        clauses.append(f"{variable} count-above {_num(t['wet_day_mm'])} {w}d >= {w}")
        if t["wet_day_op"] == ">=":
            dropped.append(
                f"wet day is >= {_num(t['wet_day_mm'])} mm in the source; the grammar only has '>'"
            )
    if t.get("threshold_kind") == "per_cell_climatology":
        if scalar_threshold is None:
            raise RegistryError(
                f"{name}: per-cell climatological threshold is not expressible in the grammar; "
                "pass scalar_threshold to compile a single-threshold approximation"
            )
        clauses.append(f"{variable} sum {w}d {t['total_op']} {_num(scalar_threshold)}")
        dropped.append("per-cell climatological threshold replaced by one scalar")
    else:
        clauses.append(f"{variable} sum {w}d {t['total_op']} {_num(t['total_mm'])}")
    c = d.get("confirm")
    if c:
        after = f" after {c['after_days']}d" if c["after_days"] > 0 else ""  # 0 = same start day
        clauses.append(
            f"{variable} sum {c['window_days']}d {c['total_op']} {_num(c['total_mm'])}{after}"
        )
    if v["mode"] == "consecutive_dry":
        clauses.append(
            f"not {variable} consecutive-below {_num(v['dry_day_mm'])} {v['dry_days']}d "
            f"within {v['follow_days']}d"
        )
    elif v["mode"] == "window_sum":
        clauses.append(
            f"not {variable} sum {v['window_days']}d < {_num(v['window_total_mm'])} "
            f"within {v['follow_days']}d"
        )
    if v["mode"] != "none" and v.get("follow_anchor", "").endswith("after_trigger_end"):
        dropped.append(
            "veto window counted from the trigger END; the grammar counts from its start"
        )
    if d.get("search", {}).get("start"):
        dropped.append(
            f"search start {d['search']['start']} (no start-date support in the grammar)"
        )
    if d.get("search", {}).get("window_days"):
        dropped.append(f"search window {d['search']['window_days']} days")
    if d["time_basis"] == "calendar_dekad":
        dropped.append("calendar dekads approximated by rolling daily windows")
    return Compiled(name=name, rule=" and ".join(clauses), exact=not dropped, dropped=dropped)

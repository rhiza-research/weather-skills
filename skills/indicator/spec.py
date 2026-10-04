"""Parse ``--rule`` into an IndicatorSpec (registry name or clause string).

Named rules are resolved from ``references/onset_definitions.toml``, a byte-identical copy of
the repository's onset-definition registry (the skill is self-contained, so it cannot import
``registry/``). Each resolved spec carries the definition id, content hash and status, so an
output records exactly which definition produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from weather_skills_core import UsageError

REGISTRY_COPY = Path(__file__).resolve().parent / "references" / "onset_definitions.toml"
_HASH_KEYS = ("time_basis", "trigger", "confirm", "veto", "search")

# Legacy alias -> registry id. A legacy alias keeps working even when the registry entry has
# parts the grammar cannot express (they are dropped and recorded, status
# ``unregistered-variant``); a registry id asked for directly is refused in that case.
LEGACY_ALIASES = {
    "icpac-onset": "icpac-onset",
    "chc-onset": "sheerwater-chc-onset",  # identical to the pre-registry alias
}

_AGGS = (
    "sum",
    "mean",
    "count-above",
    "count-below",
    "consecutive-above",
    "consecutive-below",
)
_COUNT_OR_CONSEC = frozenset(
    {"count-above", "count-below", "consecutive-above", "consecutive-below"}
)
_CONSEC = frozenset({"consecutive-above", "consecutive-below"})
_OPS = (">=", "<=", ">", "<")
_WINDOW_RE = re.compile(r"^(\d+)d$", re.IGNORECASE)
_SPLIT_RE = re.compile(r"\s+(and|or)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class Clause:
    negate: bool
    variable: str
    agg: str
    window: int
    op: str | None
    threshold: float | None
    daily_threshold: float | None
    after: int | None
    within: int | None


@dataclass(frozen=True)
class Provenance:
    """Which onset definition produced an output (stamped as ``onset_definition_*`` attrs)."""

    id: str
    hash: str
    status: str
    overrides: dict = field(default_factory=dict)

    def attrs(self) -> dict[str, str]:
        return {
            "onset_definition_id": self.id,
            "onset_definition_hash": self.hash,
            "onset_definition_status": self.status,
            "onset_definition_overrides": json.dumps(self.overrides, sort_keys=True),
        }


@dataclass(frozen=True)
class IndicatorSpec:
    source: str
    expanded: str
    combinator: str | None
    clauses: tuple[Clause, ...]
    provenance: Provenance | None = None

    def to_json(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- registry


@lru_cache(maxsize=1)
def load_registry() -> dict[str, dict]:
    """Definitions from the registry copy. Unknown keys (tunable, optimization, …) are ignored."""
    try:
        with REGISTRY_COPY.open("rb") as f:
            doc = tomllib.load(f)
    except FileNotFoundError as exc:
        raise UsageError(
            f"onset-definition registry copy missing at {REGISTRY_COPY}; "
            "run python tools/sync_definitions.py"
        ) from exc
    return dict(doc.get("definitions", {}))


def content_hash(d: dict) -> str:
    """Hash of a definition's scientific content (same recipe as registry/onset.py)."""
    keep = {k: d[k] for k in _HASH_KEYS if k in d}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


def rule_hash(rule: str) -> str:
    """Hash of a free-form clause string (provenance for ad-hoc rules)."""
    return hashlib.sha256(rule.encode()).hexdigest()[:12]


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def compile_definition(d: dict, variable: str = "precip") -> tuple[str | None, list[str]]:
    """Translate a registry definition into the clause grammar.

    Mirrors ``registry/onset.py::compile_to_indicator`` (identical rule strings for the same
    input) but never approximates: a per-cell threshold yields ``rule=None``. Returns the rule
    and the list of source features the grammar cannot express (empty when exact).
    """
    dropped: list[str] = []
    t, v = d["trigger"], d["veto"]
    w = t["window_days"]
    clauses = []
    if t.get("all_days_wet"):
        clauses.append(f"{variable} count-above {_num(t['wet_day_mm'])} {w}d >= {w}")
        if t["wet_day_op"] == ">=":
            dropped.append(
                f"wet day is >= {_num(t['wet_day_mm'])} mm in the source; the grammar only has '>'"
            )
    per_cell = t.get("threshold_kind") == "per_cell_climatology"
    if per_cell:
        dropped.append("per-cell climatological threshold (one local threshold per grid cell)")
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
    return (None if per_cell else " and ".join(clauses)), dropped


def _known_names() -> str:
    defs = load_registry()
    usable = sorted(
        set(LEGACY_ALIASES) | {n for n, d in defs.items() if not compile_definition(d)[1]}
    )
    refused = sorted(n for n in defs if n not in usable)
    return f"usable: {', '.join(usable)}; registered but not expressible: {', '.join(refused)}"


def resolve_name(name: str) -> tuple[str, Provenance] | None:
    """Resolve a legacy alias or registry id to (rule, provenance); None if not a known name."""
    defs = load_registry()
    key = name.lower()
    legacy = key in LEGACY_ALIASES
    def_id = LEGACY_ALIASES.get(key, key)
    if def_id not in defs:
        if legacy:
            raise UsageError(
                f"legacy alias {name!r} maps to registry id {def_id!r}, which is missing from "
                f"{REGISTRY_COPY.name}; run python tools/sync_definitions.py"
            )
        return None
    d = defs[def_id]
    rule, dropped = compile_definition(d)
    if rule is None or (dropped and not legacy):
        raise UsageError(
            f"onset definition {def_id!r} cannot be expressed exactly in the --rule grammar: "
            + "; ".join(dropped)
            + ". It is refused rather than silently approximated. Write an approximating "
            "clause string yourself if you accept the difference (it is recorded as ad-hoc)."
        )
    status = d["status"]
    overrides: dict = {}
    if dropped:
        # Legacy alias kept working as the rolling-window core rule; make the approximation
        # visible instead of passing it off as the registered definition.
        status = "unregistered-variant"
        overrides = {"dropped": dropped}
    return rule, Provenance(id=def_id, hash=content_hash(d), status=status, overrides=overrides)


def parse_rule(raw: str) -> IndicatorSpec:
    """Parse a registry name or a clause string joined by ``and`` / ``or``."""
    if not isinstance(raw, str) or not raw.strip():
        raise UsageError("--rule is required (a registry name or a clause string)")
    source = raw.strip()
    resolved = resolve_name(source) if " " not in source else None
    if resolved is None and " " not in source:
        raise UsageError(f"unknown rule name {source!r}. Known names — {_known_names()}")
    if resolved is not None:
        expanded, provenance = resolved
    else:
        expanded = source
        provenance = Provenance(id="ad-hoc", hash=rule_hash(source), status="unregistered")
    parts = _SPLIT_RE.split(expanded.strip())
    if not parts or not parts[0].strip():
        raise UsageError(f"could not parse --rule {source!r}")
    clauses = [parts[0]]
    combinators = []
    for i in range(1, len(parts), 2):
        combinators.append(parts[i].lower())
        if i + 1 >= len(parts) or not parts[i + 1].strip():
            raise UsageError(f"could not parse --rule {source!r}")
        clauses.append(parts[i + 1])
    unique = set(combinators)
    if len(unique) > 1:
        raise UsageError(
            "mixing 'and' and 'or' in one --rule is not supported; use a single combinator"
        )
    parsed = tuple(_parse_clause(c.strip(), source) for c in clauses)
    combinator = combinators[0] if combinators else None
    return IndicatorSpec(
        source=source,
        expanded=expanded,
        combinator=combinator,
        clauses=parsed,
        provenance=provenance,
    )


def _parse_window(token: str, source: str) -> int:
    match = _WINDOW_RE.fullmatch(token)
    if not match:
        raise UsageError(f"window {token!r} in --rule {source!r} must look like '8d'")
    days = int(match.group(1))
    if days < 1:
        raise UsageError(f"window must be >= 1d in --rule {source!r}")
    return days


def _parse_op(token: str, source: str) -> str:
    if token not in _OPS:
        raise UsageError(
            f"comparator {token!r} in --rule {source!r} must be one of {', '.join(_OPS)}"
        )
    return token


def _parse_clause(text: str, source: str) -> Clause:
    tokens = text.split()
    if not tokens:
        raise UsageError(f"empty clause in --rule {source!r}. Known names — {_known_names()}")
    i = 0
    negate = False
    if tokens[i] == "not":
        negate = True
        i += 1
    if i >= len(tokens):
        raise UsageError(f"clause {text!r} in --rule {source!r} is missing a variable")

    variable = tokens[i]
    i += 1
    if i >= len(tokens) or tokens[i] not in _AGGS:
        raise UsageError(
            f"could not parse clause {text!r} in --rule {source!r}. "
            f"Expected '{{variable}} {{agg}} …' with agg one of {', '.join(_AGGS)}. "
            f"Known names — {_known_names()}"
        )
    agg = tokens[i]
    i += 1

    daily_threshold = None
    if agg in _COUNT_OR_CONSEC:
        if i >= len(tokens):
            raise UsageError(
                f"clause {text!r} in --rule {source!r} needs a daily threshold after {agg}"
            )
        try:
            daily_threshold = float(tokens[i])
        except ValueError as exc:
            raise UsageError(
                f"daily threshold {tokens[i]!r} in --rule {source!r} is not a number"
            ) from exc
        i += 1

    if i >= len(tokens):
        raise UsageError(f"clause {text!r} in --rule {source!r} is missing a window (e.g. 8d)")
    window = _parse_window(tokens[i], source)
    i += 1

    op = None
    threshold = None
    if agg not in _CONSEC:
        if i >= len(tokens):
            raise UsageError(
                f"clause {text!r} in --rule {source!r} needs a comparator and threshold"
            )
        op = _parse_op(tokens[i], source)
        i += 1
        if i >= len(tokens):
            raise UsageError(f"clause {text!r} in --rule {source!r} is missing a threshold")
        try:
            threshold = float(tokens[i])
        except ValueError as exc:
            raise UsageError(
                f"threshold {tokens[i]!r} in --rule {source!r} is not a number"
            ) from exc
        i += 1

    after = None
    within = None
    while i < len(tokens):
        tag = tokens[i]
        if tag not in ("after", "within"):
            raise UsageError(f"unexpected {tag!r} in clause {text!r} of --rule {source!r}")
        if i + 1 >= len(tokens):
            raise UsageError(f"{tag} in --rule {source!r} needs a window (e.g. {tag} 10d)")
        days = _parse_window(tokens[i + 1], source)
        if tag == "after":
            if after is not None:
                raise UsageError(f"repeated 'after' in clause {text!r} of --rule {source!r}")
            after = days
        else:
            if within is not None:
                raise UsageError(f"repeated 'within' in clause {text!r} of --rule {source!r}")
            within = days
        i += 2

    if after is not None and within is not None:
        raise UsageError(
            f"clause {text!r} in --rule {source!r} cannot take both 'after' and 'within'"
        )
    return Clause(
        negate=negate,
        variable=variable,
        agg=agg,
        window=window,
        op=op,
        threshold=threshold,
        daily_threshold=daily_threshold,
        after=after,
        within=within,
    )

"""Action catalog loading, deterministic fingerprint, nearest-confusion map."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACTIONS_PATH = HERE / "actions.json"
PREDICATES_PATH = HERE / "predicates.py"

# Actions named as nearest confusions that are not (yet) skills on this branch. Listed so the
# confusion map can reference them without a test failure hiding a typo.
PROPOSED_OR_EXTERNAL = {
    "regrid": "proposed; absent on dev (unmerged branch ai/1.7-regrid-skill)",
    "make-climatology": "proposed (spec v2); absent on dev",
    "exceedance-probability": "open upstream PR #114",
}


def load_actions(path: Path = ACTIONS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["actions"]


def catalog_fingerprint(
    actions_path: Path = ACTIONS_PATH, predicates_path: Path = PREDICATES_PATH
) -> str:
    """sha256 over canonical action contracts + the eligibility source. Recorded on every routing
    event as action_catalog_hash, so a trace can be matched to the ontology that produced it."""
    actions = json.loads(actions_path.read_text(encoding="utf-8"))
    canon = json.dumps(actions, sort_keys=True, separators=(",", ":"))
    src = predicates_path.read_bytes().replace(b"\r\n", b"\n")
    h = hashlib.sha256()
    h.update(canon.encode())
    h.update(b"\0")
    h.update(src)
    return h.hexdigest()[:16]


def confusion_map(actions: dict | None = None) -> dict[str, list[str]]:
    """Machine-readable nearest-neighbour map: the high-value hard negatives for a CLM."""
    actions = actions or load_actions()
    return {k: sorted(v.get("nearest_confusions", [])) for k, v in sorted(actions.items())}

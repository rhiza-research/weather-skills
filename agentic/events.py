"""Opt-in routing event log: clean (state, eligible actions, choice, outcome) examples.

Off unless WEATHER_SKILLS_ROUTING_LOG names a JSONL file. Never records credentials, file paths
or raw conversation text: states are canonical (agentic.state), runtime arguments are dropped
and only declared scientific parameters are kept. Logging never changes a skill's output.

Training distinctions (spec clm_instrumentation.training_distinctions) are explicit fields, so a
contract violation is never confused with a learned hard negative:
  positive   -- selected action, transition verified
  hard_neg   -- ELIGIBLE actions not selected (derive at training time from eligible_actions)
  invalid    -- invalid_actions with reason codes (deterministic; never a training negative)
  failed     -- selected action whose verification failed
  recovery   -- recovery_of names the failed step this one recovers from
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic.catalog import catalog_fingerprint
from agentic.predicates import Verdict

ENV_VAR = "WEATHER_SKILLS_ROUTING_LOG"
SELECTION_SOURCES = {"llm", "clm", "rule", "human", "reference"}
EVENT_SCHEMA_VERSION = "1"
# Path SHAPES, not any slash: "mm/day" is a legitimate scientific value.
_PATHLIKE = re.compile(
    r"^([A-Za-z]:[\\/]|[\\/]|\.{1,2}[\\/]|~)|\.(zarr|png|nc|json|geojson|csv|parquet)$", re.I
)


@dataclass
class RoutingEvent:
    workflow_id: str
    step_index: int
    state_before: dict[str, Any]
    eligible_actions: list[str]
    selected_action: str
    selection_source: str
    scientific_parameters: dict[str, Any] = field(default_factory=dict)
    invalid_actions: dict[str, list[str]] = field(default_factory=dict)  # action -> reason codes
    candidate_scores: dict[str, float] | None = None
    state_after: dict[str, Any] | None = None
    verification: dict[str, bool] | None = None
    reward: float | None = None
    recovery_of: int | None = None
    action_catalog_hash: str = ""
    schema_version: str = EVENT_SCHEMA_VERSION

    def __post_init__(self):
        if self.selection_source not in SELECTION_SOURCES:
            raise ValueError(f"selection_source must be one of {sorted(SELECTION_SOURCES)}")
        if self.selected_action not in self.eligible_actions and self.selection_source != "human":
            raise ValueError(
                f"{self.selected_action!r} is not eligible; only a human override may log it"
            )
        for k, v in self.scientific_parameters.items():
            if isinstance(v, str) and _PATHLIKE.search(v):
                raise ValueError(
                    f"scientific parameter {k!r} looks like a file path; log scientific values "
                    "only (runtime paths are never recorded)"
                )
        if not self.action_catalog_hash:
            self.action_catalog_hash = catalog_fingerprint()

    @property
    def outcome(self) -> str:
        if self.verification is None:
            return "unverified"
        return "positive" if all(self.verification.values()) else "failed"

    def to_json(self) -> str:
        d = asdict(self)
        d["outcome"] = self.outcome
        return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


def invalid_from_verdicts(verdicts: dict[str, Verdict]) -> dict[str, list[str]]:
    return {
        a: sorted({r.code for r in v.reasons})
        for a, v in sorted(verdicts.items())
        if not v.eligible
    }


class RoutingLogger:
    """Append-only JSONL writer; a no-op when the env var is unset."""

    def __init__(self, path: str | os.PathLike | None = None, workflow_id: str | None = None):
        target = path if path is not None else os.environ.get(ENV_VAR)
        self.path = Path(target) if target else None
        self.workflow_id = workflow_id or uuid.uuid4().hex[:12]
        self.step = 0

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def log(self, **kw) -> RoutingEvent | None:
        if not self.enabled:
            return None
        ev = RoutingEvent(workflow_id=self.workflow_id, step_index=self.step, **kw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(ev.to_json() + "\n")
        self.step += 1
        return ev

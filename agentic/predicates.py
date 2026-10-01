"""Deterministic eligibility: A_eligible(s) = {a : preconditions(s, a)}. Never learned.

Three predicate classes, resolved in order (spec: meta_quality.predicate_classes):
  syntactic  -- required artifacts / dims / parameters exist
  scientific -- the operation is scientifically valid for the semantic state
  resource   -- executable here (credentials, optional dependencies)

Every rejection carries a stable reason code, so invalid actions stay distinct from learned
hard negatives in routing logs. Each rule states whether the SKILL CODE enforces it
("code") or whether only this layer does ("layer") -- the latter are where a skill would
otherwise fail silently (e.g. difference on mismatched grids), and are verified against the
real skills in tests/test_predicates_conformance.py.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SYNTACTIC, SCIENTIFIC, RESOURCE = "syntactic", "scientific", "resource"


@dataclass(frozen=True)
class Reason:
    klass: str
    code: str
    enforced_by: str  # "code" | "layer"
    message: str


@dataclass
class Verdict:
    action: str
    eligible: bool
    bindings: list[str] = field(default_factory=list)  # artifact slots the action may apply to
    reasons: list[Reason] = field(default_factory=list)


def _arts(state) -> dict[str, dict[str, Any]]:
    return state.get("artifacts", {})


def _vars(a):
    return a.get("variables", {}).values()


def _is_amount(a) -> bool:
    return any(v.get("units_kind") == "precip_amount" for v in _vars(a))


def _aggregated(a) -> bool:
    return any(v.get("aggregation_period") for v in _vars(a))


def _gridded(a) -> bool:
    return a.get("grid", {}).get("kind") in ("rectilinear", "curvilinear")


def _res(a):
    g = a.get("grid", {})
    return (g.get("dlat"), g.get("dlon"))


def _same_spacing(a, b, rtol=0.01) -> bool:
    """Same tolerance verify uses (np.isclose(..., rtol=0.01)); unknown spacing counts as same."""
    for x, y in zip(_res(a), _res(b), strict=True):
        if x is None or y is None:
            continue
        if abs(x - y) > rtol * max(abs(x), abs(y)) + 1e-6:
            return False
    return True


# -- per-action unary predicates: (slot, artifact, state) -> list[Reason] (empty = ok) --------


def _p_aggregate(slot, a, state):
    rs = []
    if a["time_representation"] not in ("valid_time", "init+lead"):
        rs.append(Reason(SYNTACTIC, "NO_TIME_AXIS", "code", f"{slot}: no time or lead axis"))
    period = state.get("objective", {}).get("temporal_scale")
    if period == "monthly" and a["time_representation"] == "init+lead":
        rs.append(
            Reason(
                SCIENTIFIC,
                "MONTHLY_ON_LEAD",
                "code",
                f"{slot}: monthly aggregation is not defined on a lead axis; step-to-time first",
            )
        )
    return rs


def _p_totals(slot, a, state):
    rs = []
    if _is_amount(a):
        rs.append(Reason(SCIENTIFIC, "ALREADY_AMOUNT", "code", f"{slot}: already a period amount"))
    elif not _aggregated(a):
        rs.append(
            Reason(
                SCIENTIFIC,
                "NOT_AGGREGATED",
                "code",
                f"{slot}: no aggregation_period; aggregate-temporal first",
            )
        )
    return rs


def _p_deacc(slot, a, state):
    rs = []
    if a["time_representation"] != "init+lead":
        rs.append(
            Reason(SYNTACTIC, "NO_LEAD_AXIS", "code", f"{slot}: not a forecast with a lead axis")
        )
    if not _is_amount(a):
        rs.append(
            Reason(
                SCIENTIFIC, "ALREADY_RATE", "code", f"{slot}: already a rate; do not deaccumulate"
            )
        )
    return rs


def _p_step_to_time(slot, a, state):
    if a["time_representation"] != "init+lead":
        return [Reason(SYNTACTIC, "NO_LEAD_AXIS", "code", f"{slot}: no lead axis to convert")]
    return []


def _p_clip(slot, a, state):
    rs = []
    if not state.get("objective", {}).get("region"):
        rs.append(Reason(SYNTACTIC, "NO_REGION", "layer", "objective has no region"))
    if a.get("grid", {}).get("kind") not in ("rectilinear", "points"):
        rs.append(
            Reason(SCIENTIFIC, "GRID_UNSUPPORTED", "layer", f"{slot}: grid kind not clippable")
        )
    return rs


def _p_coarsen(slot, a, state):
    return (
        []
        if a.get("grid", {}).get("kind") == "rectilinear"
        else [
            Reason(
                SCIENTIFIC, "GRID_UNSUPPORTED", "code", f"{slot}: needs a rectilinear lat/lon grid"
            )
        ]
    )


def _p_summarize(slot, a, state):
    return (
        []
        if a.get("dims")
        else [Reason(SYNTACTIC, "NO_DIMS", "code", f"{slot}: nothing to summarize")]
    )


def _p_calendar(slot, a, state):
    if a["time_representation"] != "valid_time":
        return [Reason(SYNTACTIC, "NO_TIME_AXIS", "code", f"{slot}: needs a valid-time axis")]
    if a.get("calendar") in ("standard", "gregorian", "proleptic_gregorian"):
        return [
            Reason(
                SCIENTIFIC, "ALREADY_STANDARD", "layer", f"{slot}: already on a standard calendar"
            )
        ]
    return []


def _p_indicator(slot, a, state):
    if a.get("data_interval") not in ("1 day", "1 days", None):
        return [Reason(SCIENTIFIC, "NOT_DAILY", "code", f"{slot}: indicator needs daily input")]
    return []


UNARY: dict[str, Callable] = {
    "aggregate-temporal": _p_aggregate,
    "convert-to-totals": _p_totals,
    "deaccumulate": _p_deacc,
    "step-to-time": _p_step_to_time,
    "clip-region": _p_clip,
    "coarsen": _p_coarsen,
    "downscale": _p_coarsen,
    "summarize-dim": _p_summarize,
    "convert-calendar": _p_calendar,
    "indicator": _p_indicator,
    "unit-convert": lambda s, a, st: [],
    "select": _p_summarize,
}


# -- pairwise predicates -----------------------------------------------------------------


def _pair(state, want=("forecast", "observation")):
    arts = _arts(state)
    fc = next((k for k, a in arts.items() if a["role"] == want[0] or k == want[0]), None)
    ob = next((k for k, a in arts.items() if a["role"] == want[1] or k in (want[1], "obs")), None)
    return fc, ob


def _p_verify(state) -> Verdict:
    v = Verdict("verify", False)
    fc, ob = _pair(state)
    arts = _arts(state)
    if not fc or not ob:
        v.reasons.append(
            Reason(SYNTACTIC, "NEEDS_FORECAST_AND_OBS", "code", "verify needs --forecast and --obs")
        )
        return v
    f, o = arts[fc], arts[ob]
    if f["time_representation"] == "init+lead" and o["time_representation"] == "valid_time":
        v.reasons.append(
            Reason(
                SCIENTIFIC,
                "LEAD_VS_VALID_TIME",
                "code",
                "forecast still on lead axis; step-to-time first",
            )
        )
    if _gridded(f) and _gridded(o) and not _same_spacing(f, o):
        v.reasons.append(
            Reason(
                SCIENTIFIC,
                "GRID_MISMATCH",
                "code",
                "grid spacing differs; coarsen --obs --reference-grid first",
            )
        )
    if o.get("grid", {}).get("kind") == "points":
        v.reasons.append(
            Reason(SCIENTIFIC, "POINTS_NOT_SUPPORTED", "code", "verify is cell-by-cell on grids")
        )
    v.eligible, v.bindings = not v.reasons, [fc, ob]
    return v


def _p_difference(state) -> Verdict:
    v = Verdict("difference", False)
    grids = [k for k, a in _arts(state).items() if _gridded(a)]
    if len(grids) != 2:
        v.reasons.append(
            Reason(SYNTACTIC, "NEEDS_TWO_GRIDS", "code", "difference takes exactly two inputs")
        )
        return v
    a, b = (_arts(state)[k] for k in grids)
    if not _same_spacing(a, b):
        v.reasons.append(
            Reason(
                SCIENTIFIC,
                "GRID_MISMATCH",
                "layer",
                "grids differ; the skill would run and silently keep only the overlap",
            )
        )
    if a["time_representation"] != b["time_representation"]:
        v.reasons.append(
            Reason(
                SCIENTIFIC,
                "TIME_REPRESENTATION_MISMATCH",
                "layer",
                "one input is on lead time, the other on valid time",
            )
        )
    if a.get("calendar") != b.get("calendar"):
        v.reasons.append(
            Reason(
                SCIENTIFIC, "CALENDAR_MISMATCH", "layer", "calendars differ; convert-calendar first"
            )
        )
    v.eligible, v.bindings = not v.reasons, grids
    return v


def _p_standardize(state) -> Verdict:
    v = Verdict("standardize-anomaly", False)
    arts = _arts(state)
    clim = next(
        (k for k, a in arts.items() if a["role"] == "climatology" or k == "climatology"), None
    )
    data = next((k for k in arts if k != clim), None)
    if not clim or not data:
        v.reasons.append(
            Reason(
                SYNTACTIC,
                "NEEDS_DATA_AND_CLIMATOLOGY",
                "code",
                "needs a data and a climatology input",
            )
        )
        return v
    names = set(arts[clim]["variables"])
    if not any(n.endswith("_avg") for n in names) or not any(n.endswith("_std") for n in names):
        v.reasons.append(
            Reason(
                SCIENTIFIC,
                "CLIMATOLOGY_NEEDS_AVG_STD",
                "code",
                "climatology lacks *_avg / *_std variables",
            )
        )
    v.eligible, v.bindings = not v.reasons, [data, clim]
    return v


PAIRWISE: dict[str, Callable] = {
    "verify": _p_verify,
    "difference": _p_difference,
    "standardize-anomaly": _p_standardize,
}

# -- resource predicates -------------------------------------------------------------------

# Credentials the skill code actually requires (read from each fetch.py, not its SKILL.md):
# ecmwf-fetch defaults ECMWF_DATASTORES_URL itself, so only the key is needed; smap-fetch
# accepts EARTHDATA_* env vars OR a urs.earthdata.nasa.gov entry in ~/.netrc.
CREDENTIALS = {
    "ecmwf-fetch": (("ECMWF_DATASTORES_KEY",),),
    "tahmo-fetch": (("TAHMO_API_USERNAME", "TAHMO_API_PASSWORD"),),
    "smap-fetch": (("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"), ("netrc:urs.earthdata.nasa.gov",)),
}


def _has(requirement: str, env) -> bool:
    if requirement.startswith("netrc:"):
        host = requirement.split(":", 1)[1]
        path = Path(env.get("NETRC") or Path.home() / ".netrc")
        try:
            return path.is_file() and host in path.read_text(errors="ignore")
        except OSError:
            return False
    return bool(env.get(requirement))


def _resource(action: str, env=None) -> list[Reason]:
    """Eligible if ANY alternative credential set is fully present."""
    env = os.environ if env is None else env
    alternatives = CREDENTIALS.get(action)
    if not alternatives or any(all(_has(r, env) for r in alt) for alt in alternatives):
        return []
    wanted = " or ".join("+".join(alt) for alt in alternatives)
    return [Reason(RESOURCE, "MISSING_CREDENTIAL", "code", f"{action} needs {wanted}")]


def evaluate(state: dict[str, Any], action: str, env=None) -> Verdict:
    """One action's verdict with every reason code (not just the first)."""
    if action in PAIRWISE:
        v = PAIRWISE[action](state)
    elif action in UNARY:
        v = Verdict(action, False)
        per_slot = {slot: UNARY[action](slot, a, state) for slot, a in sorted(_arts(state).items())}
        v.bindings = [s for s, rs in per_slot.items() if not rs]
        if not v.bindings:
            v.reasons = [r for rs in per_slot.values() for r in rs] or [
                Reason(SYNTACTIC, "NO_ARTIFACTS", "code", "no input artifact")
            ]
        v.eligible = bool(v.bindings)
    elif action.endswith("-fetch"):
        # Fetchers take no input artifact. Whether a source carries the requested variable,
        # region or period is NOT modelled here (it is data-catalog knowledge); only
        # credentials are gated, below.
        v = Verdict(action, True)
    else:
        v = Verdict(
            action,
            False,
            reasons=[Reason(SYNTACTIC, "UNMODELED", "layer", "no contract predicate")],
        )
    res = _resource(action, env)
    if res:
        v.eligible, v.reasons = False, v.reasons + res
    return v


def eligible_actions(
    state: dict[str, Any], actions, env=None
) -> tuple[list[str], dict[str, Verdict]]:
    verdicts = {a: evaluate(state, a, env) for a in sorted(actions)}
    return [a for a, v in verdicts.items() if v.eligible], verdicts

"""Canonical scientific state: what a router (LLM, CLM, rule, planner) may condition on.

Built from the dataset's *semantics*, never its representation. Dimension names resolve through
the weather-skills-core ontology (``standard_dataset.ALIASES`` + CF detection), so ``latitude`` vs
``lat``, ``number`` vs ``member``, ``step`` vs ``prediction_timedelta``, dimension order, chunk
layout, variable order and file paths all serialize identically. Variable identity comes from
``units.classify_variable`` (CF standard_name first, then name hints), rate-vs-amount from the
stamped units, and processing state from the attributes the skills themselves stamp
(``aggregation_period``, ``data_interval``, ``cell_methods``, ``weather_skills_history``).

Nothing here reads array values, so building a state never materialises data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from weather_skills_core import standard_dataset as std
from weather_skills_core.provenance import HISTORY_ATTR, SOURCE_ATTR, chain_is_intact, coerce_chain
from weather_skills_core.units import (
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
    classify_variable,
    kind_from_units,
    variable_units,
)

STATE_SCHEMA_VERSION = "1"
HISTORY_WINDOW = 8  # "relevant prior skill transitions only"

_ONTOLOGY_ORDER = [
    std.MEMBER,
    std.INIT_TIME,
    std.PREDICTION_TIMEDELTA,
    std.TIME,
    std.DAY_OF_YEAR,
    std.VERTICAL,
    std.POINT_ID,
    std.LAT,
    std.LON,
    std.Y,
    std.X,
]


def _ontology_dims(ds) -> list[str]:
    present = [d for d in _ONTOLOGY_ORDER if std.has_dim(ds, d)]
    covered = set()
    for name in ds.dims:
        alias = std.ALIASES.get(str(name))
        if alias:
            covered.add(str(name))
    for d in present:
        if d == std.LAT or d == std.LON:
            try:
                covered.update(std.detect_spatial_dims(ds))
            except Exception:  # noqa: BLE001 -- detection failure just means "not present"
                pass
        if d == std.TIME:
            try:
                covered.add(std.detect_time_dim(ds))
            except Exception:  # noqa: BLE001
                pass
    other = sorted(f"other:{n}" for n in map(str, ds.dims) if n not in covered)
    return present + other


def _time_representation(dims: list[str]) -> str:
    if std.PREDICTION_TIMEDELTA in dims:
        return "init+lead"
    if std.DAY_OF_YEAR in dims:
        return "climatological"
    if std.TIME in dims:
        return "valid_time"
    return "none"


def _grid(ds) -> dict[str, Any]:
    if std.has_dim(ds, std.POINT_ID):
        return {"kind": "points"}
    try:
        lat, lon = std.detect_spatial_dims(ds)
    except Exception:  # noqa: BLE001
        return {"kind": "none"}
    out: dict[str, Any] = {"kind": "rectilinear"}
    if ds[lat].ndim != 1 or ds[lon].ndim != 1:
        out["kind"] = "curvilinear"
    else:
        for axis, name in (("dlat", lat), ("dlon", lon)):
            v = ds[name].values
            if v.size > 1:
                out[axis] = round(float(abs(v[1] - v[0])), 6)
    out["bounds"] = bool(ds[lat].attrs.get("bounds") and ds[lon].attrs.get("bounds"))
    out["grid_mapping"] = any("grid_mapping" in ds[v].attrs for v in ds.data_vars)
    return out


def _calendar(ds) -> str:
    for name in ("time", "init_time", "valid_time"):
        if name in ds.coords:
            enc = ds[name].encoding.get("calendar") or ds[name].attrs.get("calendar")
            if enc:
                return str(enc)
            dtype = str(ds[name].dtype)
            if dtype.startswith("datetime64"):
                return "standard"
            if dtype == "object":
                first = ds[name].values.ravel()[:1]
                cal = getattr(first[0], "calendar", None) if first.size else None
                return str(cal or "cftime")
    return "none"


def _variable(ds, name) -> dict[str, Any]:
    da = ds[name]
    units = variable_units(da) or da.attrs.get("units")
    sn = da.attrs.get("standard_name")
    kind = classify_variable(str(name), units=units, standard_name=sn)
    ukind = kind_from_units(units) if isinstance(units, str) and units.strip() else None
    return {
        "semantic": kind or "unclassified",
        "standard_name": sn,
        "units": units,
        "units_kind": ukind,
        "cell_methods": da.attrs.get("cell_methods"),
        "aggregation_period": da.attrs.get(AGGREGATION_PERIOD_ATTR),
    }


def _history(ds) -> dict[str, Any]:
    raw = ds.attrs.get(HISTORY_ATTR)
    chain = coerce_chain(raw, "state") if isinstance(raw, str) else (raw or [])
    chain = chain or []
    skills = [e.get("skill") for e in chain if isinstance(e, dict)]
    return {
        "recent_skills": skills[-HISTORY_WINDOW:],
        "length": len(skills),
        "intact": bool(chain_is_intact(chain)) if chain else False,
        "source": ds.attrs.get(SOURCE_ATTR),
    }


def artifact_state(ds, role: str) -> dict[str, Any]:
    """Canonical state for one dataset. ``role`` is forecast|observation|reanalysis|climatology|derived."""
    dims = _ontology_dims(ds)
    variables = {str(v): _variable(ds, v) for v in sorted(map(str, ds.data_vars))}
    return {
        "role": role,
        "dataset_type": std.detect_type(ds),
        "dims": dims,
        "time_representation": _time_representation(dims),
        "ensemble": {"present": std.MEMBER in dims},
        "grid": _grid(ds),
        "calendar": _calendar(ds),
        "data_interval": ds.attrs.get(DATA_INTERVAL_ATTR),
        "variables": variables,
        "history": _history(ds),
    }


def canonical_state(
    objective: dict[str, Any],
    artifacts: dict[str, Any],
    requirements: dict[str, Any] | None = None,
    recent_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Full routing state. ``artifacts`` maps a STABLE slot name (e.g. "forecast", "obs") to an
    xarray Dataset or an already-built artifact_state dict -- never to a path.

    objective keys (spec minimum): intent, quantity, variables, region, temporal_scale, output.
    """
    arts = {}
    for slot in sorted(artifacts):
        a = artifacts[slot]
        arts[slot] = a if isinstance(a, dict) else artifact_state(a, role=slot)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "objective": {k: objective[k] for k in sorted(objective)},
        "artifacts": arts,
        "requirements": {k: v for k, v in sorted((requirements or {}).items())},
        "history": list(recent_actions or [])[-HISTORY_WINDOW:],
    }


def to_json(state: dict[str, Any]) -> str:
    """Deterministic serialization (sorted keys, no whitespace variance)."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)


def state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(to_json(state).encode()).hexdigest()[:16]

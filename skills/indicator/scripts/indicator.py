# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Apply a boolean indicator (or ensemble probability) to a daily standard dataset."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    data_interval_of,
    infer_timestep,
    kind_from_units,
    parse_aggregation_period,
    rate_to_total,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


def _load_local(name: str):
    path = Path(__file__).resolve().parent.parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"indicator_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_spec = _load_local("spec")
_ops = _load_local("operators")
parse_rule = _spec.parse_rule


def _axis(ds, time_dim: str | None) -> str:
    if time_dim:
        if time_dim not in ds.dims:
            raise UsageError(f"--time-dim {time_dim!r} not in dataset dims {list(ds.dims)}")
        return time_dim
    if "time" in ds.dims and ds.sizes["time"] > 1:
        return "time"
    if "step" in ds.dims:
        return "step"
    if "time" in ds.dims:
        return "time"
    raise UsageError(
        "could not identify a daily time or step dim; pass --time-dim. "
        f"Available dims: {list(ds.dims)}"
    )


def _step_days(ds, dim: str) -> int:
    """Step of ``dim`` in whole days (1 = daily, 7 = weekly)."""
    stamped = data_interval_of(ds)
    if stamped:
        days = float(parse_aggregation_period(stamped).to("day").magnitude)
    elif ds.sizes.get(dim, 0) >= 2:
        days = float(infer_timestep(ds, dim).to("day").magnitude)
    else:
        raise UsageError(
            "indicator needs data_interval (e.g. '1 day', '7 day') or at least two samples."
        )
    if days < 1 or abs(days - round(days)) > 0.05:
        raise UsageError(
            f"indicator requires a whole-day step (daily or weekly); got {days:g} day. "
            "Run aggregate-temporal --period daily or --period weekly first."
        )
    return round(days)


@weather_skill(
    name="indicator",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--rule",
    required=True,
    help=(
        "Named alias (icpac-onset, chc-onset) or one string of clauses joined by "
        "and/or, e.g. 'precip sum 8d >= 25'."
    ),
)
@weather_skill.argument("--variable", "-v", help="Override the variable named in --rule.")
@weather_skill.argument("--time-dim", help="Daily axis (default: time, else step).")
@weather_skill.argument(
    "--detect",
    choices=["first", "any"],
    default=None,
    help="Collapse the time axis: first True coordinate, or True anywhere.",
)
@weather_skill.argument(
    "--cumulative",
    action="store_true",
    help="Once True, stay True (has the event happened yet?).",
)
@weather_skill.argument(
    "--probability",
    action="store_true",
    help="Ensemble fraction True (mean over number). No-op if there is no number dim.",
)
def indicator(ds, rule, variable, time_dim, detect, cumulative, probability, **kwargs):
    """Apply a boolean indicator (or ensemble probability) to a daily standard dataset."""
    spec = parse_rule(rule)
    dim = _axis(ds, time_dim)
    step_days = _step_days(ds, dim)
    # Precip rates → mm per step, so thresholds are totals (daily mm day-1 is unchanged).
    for name in {variable or c.variable for c in spec.clauses} & set(ds.data_vars):
        if step_days > 1 and kind_from_units(variable_units(ds[name]) or "") == "precip":
            ds[name] = rate_to_total(ds[name], f"{step_days} day").pint.dequantify()
    mask = _ops.evaluate_spec(ds, spec, dim, variable, step_days)
    out = _ops.apply_reductions(
        mask, dim, cumulative=bool(cumulative), detect=detect, probability=bool(probability)
    )
    payload = json.dumps(spec.to_json(), default=str)
    for name in out.data_vars:
        attrs = dict(out[name].attrs)
        attrs["long_name"] = f"Indicator: {spec.source}"
        if name in ("indicator", "probability"):
            attrs["units"] = "1"
        attrs["indicator_spec"] = payload
        out[name].attrs = attrs
    out.attrs["indicator_rule"] = spec.source
    out.attrs["indicator_expanded"] = spec.expanded
    names = ", ".join(out.data_vars)
    print(f"indicator  {spec.source}  ({names})")
    return out


if __name__ == "__main__":
    indicator()

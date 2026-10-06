# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Apply a boolean indicator (or ensemble probability) to a daily or weekly standard dataset."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    data_interval_of,
    format_duration,
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


_NOT_WHOLE_DAYS = (
    "indicator requires a uniform step of whole days (daily or weekly); {what}. "
    "Run aggregate-temporal --period daily or --period weekly first."
)


def _step_days(ds, dim: str) -> int:
    """Spacing of ``dim`` in whole days (1 = daily, 7 = weekly); refuse anything else."""
    stamped = data_interval_of(ds)
    if stamped:
        days = float(parse_aggregation_period(stamped).to("day").magnitude)
        what = f"data_interval is {stamped!r}"
    elif ds.sizes.get(dim, 0) < 2:
        raise UsageError(
            "indicator needs a step it can measure; stamp data_interval (e.g. '1 day', "
            "'7 day') or pass a series with at least two samples."
        )
    else:
        dt = infer_timestep(ds, dim)
        days = float(dt.to("day").magnitude)
        what = f"spacing on {dim!r} is {format_duration(dt)}"
    if days < 1 - 1e-6 or abs(days - round(days)) > 1e-6:
        raise UsageError(_NOT_WHOLE_DAYS.format(what=what))
    step = round(days)
    if ds.sizes.get(dim, 0) >= 2:
        # Windows are counted in samples, so every gap must equal the step.
        ticks = np.asarray(ds[dim].values)
        kind = "datetime64" if np.issubdtype(ticks.dtype, np.datetime64) else "timedelta64"
        try:
            gaps = np.diff(ticks.astype(f"{kind}[ns]").astype(np.int64))
        except (TypeError, ValueError) as exc:
            raise UsageError(f"{dim!r} must be a datetime or timedelta axis: {exc}") from None
        if not np.all(gaps == step * 86_400 * 10**9):
            raise UsageError(
                _NOT_WHOLE_DAYS.format(what=f"{dim!r} is not evenly spaced at {step} day")
            )
    return step


def _per_step_amounts(ds, names, step_days: int):
    """Precip rates → amount per step (mm), so thresholds read as totals per window.

    Daily ``mm day-1`` is unchanged; weekly ``mm day-1`` becomes mm per week.
    Amounts and non-precip variables pass through.
    """
    for name in names:
        if name not in ds or kind_from_units(variable_units(ds[name]) or "") != "precip":
            continue
        attrs = dict(ds[name].attrs)
        total = rate_to_total(ds[name], f"{step_days} day").pint.dequantify()
        attrs["units"] = total.attrs.get("units", "mm")
        ds[name] = total.assign_attrs(attrs)
    return ds


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
@weather_skill.argument("--time-dim", help="Daily or weekly axis (default: time, else step).")
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
    """Apply a boolean indicator (or ensemble probability) to a daily or weekly dataset."""
    spec = parse_rule(rule)
    dim = _axis(ds, time_dim)
    step_days = _step_days(ds, dim)
    names = {variable or clause.variable for clause in spec.clauses}
    ds = _per_step_amounts(ds, names, step_days)
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

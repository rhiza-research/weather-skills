# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Apply a yes/no weather rule to each day or week of a Zarr, optionally reduced to an onset date or a probability."""

import json
import re
import sys
from dataclasses import asdict, dataclass

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    data_interval_of,
    infer_timestep,
    kind_from_units,
    parse_aggregation_period,
    rate_to_total,
    to_standard_units,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"


# --- --rule parsing ---------------------------------------------------------

ALIASES = {
    "icpac-onset": ("precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d"),
    "chc-onset": "precip sum 10d > 25 and precip sum 20d > 20 after 10d",
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
_WINDOW_RE = re.compile(r"^(\d+)([dw])$", re.IGNORECASE)
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
class IndicatorSpec:
    source: str
    expanded: str
    combinator: str | None
    clauses: tuple[Clause, ...]

    def to_json(self) -> dict:
        return asdict(self)


def parse_rule(raw: str) -> IndicatorSpec:
    """Parse a named alias or a clause string joined by ``and`` / ``or``."""
    if not isinstance(raw, str) or not raw.strip():
        raise UsageError("--rule is required (a named alias or a clause string)")
    source = raw.strip()
    key = source.lower()
    expanded = ALIASES.get(key, source)
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
    return IndicatorSpec(source=source, expanded=expanded, combinator=combinator, clauses=parsed)


def _parse_window(token: str, source: str) -> int:
    match = _WINDOW_RE.fullmatch(token)
    if not match:
        raise UsageError(f"window {token!r} in --rule {source!r} must look like '8d' or '1w'")
    days = int(match.group(1)) * (7 if match.group(2).lower() == "w" else 1)
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
        raise UsageError(
            f"empty clause in --rule {source!r}. Known aliases: {', '.join(sorted(ALIASES))}"
        )
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
        aliases = ", ".join(sorted(ALIASES))
        raise UsageError(
            f"could not parse clause {text!r} in --rule {source!r}. "
            f"Expected '{{variable}} {{agg}} …' with agg one of {', '.join(_AGGS)}. "
            f"Known aliases: {aliases}"
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


# --- evaluation and reductions ------------------------------------------------

_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def evaluate_spec(
    ds, spec, dim: str, variable_override: str | None, step_days: int = 1
) -> xr.DataArray:
    """Return a 0/1/NaN mask aligned to ``dim`` (ensemble ``number`` kept).

    Clause windows are in days; ``step_days`` is the spacing of ``dim``.
    """
    masks = []
    for clause in spec.clauses:
        name = variable_override or clause.variable
        if name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --input. Available: {list(ds.data_vars)}"
            )
        masks.append(_evaluate_clause(ds[name], clause, dim, step_days))
    out = masks[0]
    combine = spec.combinator or "and"
    for other in masks[1:]:
        out = _combine(out, other, combine)
    return out.astype("float32")


def apply_reductions(
    mask: xr.DataArray,
    dim: str,
    *,
    cumulative: bool,
    detect: str | None,
    probability: bool,
) -> xr.Dataset:
    """Apply ``--cumulative``, ``--detect``, ``--probability`` in that order."""
    if detect is not None and cumulative:
        raise UsageError("--detect cannot be combined with --cumulative")
    if detect == "first" and probability:
        raise UsageError(
            "--detect first writes dates, not 0/1; use --detect any --probability "
            "or --cumulative --probability"
        )

    field = mask
    if cumulative:
        field = _cumulative(field, dim)

    if detect == "first":
        return _detect_first(field, dim)
    if detect == "any":
        field = _detect_any(field, dim)

    if probability:
        field = _probability(field)
        name = "probability"
    else:
        name = "indicator"
    field = field.astype("float32")
    field.name = name
    return field.to_dataset()


def _strip_pint(da: xr.DataArray) -> xr.DataArray:
    if getattr(getattr(da, "pint", None), "units", None) is not None:
        return da.pint.dequantify()
    return da


def _steps(days: int, step_days: int) -> int:
    if days % step_days:
        raise UsageError(f"window {days}d is not a whole number of {step_days}-day steps")
    return days // step_days


def _evaluate_clause(da: xr.DataArray, clause, dim: str, step_days: int) -> xr.DataArray:
    da = _strip_pint(da)
    core = _core_mask(da, clause, dim, _steps(clause.window, step_days))
    if clause.after is not None:
        core = core.shift({dim: -_steps(clause.after, step_days)})
    if clause.within is not None:
        core = _within(core, dim, _steps(clause.within, step_days))
    if clause.negate:
        core = xr.where(core.isnull(), np.nan, 1.0 - core)
    return core.astype("float32")


def _core_mask(da: xr.DataArray, clause, dim: str, window: int) -> xr.DataArray:
    agg = clause.agg
    if agg in ("sum", "mean"):
        rolled = _left_roll(da, dim, window, agg)
        compared = _OPS[clause.op](rolled, clause.threshold)
        return xr.where(rolled.isnull(), np.nan, compared.astype("float32"))
    daily = clause.daily_threshold
    if agg == "count-above":
        flag = xr.where(da.isnull(), np.nan, (da > daily).astype("float32"))
        rolled = _left_roll(flag, dim, window, "sum")
        compared = _OPS[clause.op](rolled, clause.threshold)
        return xr.where(rolled.isnull(), np.nan, compared.astype("float32"))
    if agg == "count-below":
        flag = xr.where(da.isnull(), np.nan, (da < daily).astype("float32"))
        rolled = _left_roll(flag, dim, window, "sum")
        compared = _OPS[clause.op](rolled, clause.threshold)
        return xr.where(rolled.isnull(), np.nan, compared.astype("float32"))
    if agg == "consecutive-above":
        flag = xr.where(da.isnull(), np.nan, (da > daily).astype("float32"))
        rolled = _left_roll(flag, dim, window, "sum")
        return xr.where(rolled.isnull(), np.nan, (rolled == window).astype("float32"))
    if agg == "consecutive-below":
        flag = xr.where(da.isnull(), np.nan, (da < daily).astype("float32"))
        rolled = _left_roll(flag, dim, window, "sum")
        return xr.where(rolled.isnull(), np.nan, (rolled == window).astype("float32"))
    raise UsageError(f"unsupported agg {agg!r}")


def _left_roll(da: xr.DataArray, dim: str, window: int, method: str) -> xr.DataArray:
    """Trailing rolling window, then shift labels to the left edge (forward window)."""
    if window < 1:
        raise UsageError(f"rolling window must be >= 1; got {window}")
    axis = da[dim]
    if window == 1:
        return da
    rolled = da.rolling({dim: window}, min_periods=window, center=False)
    if method == "sum":
        out = rolled.sum(skipna=False)
    elif method == "mean":
        out = rolled.mean(skipna=False)
    else:
        raise UsageError(f"unsupported rolling method {method!r}")
    out = out.isel({dim: slice(window - 1, None)})
    # Label each window by its first sample (works for any uniform step).
    out = out.assign_coords({dim: axis.values[: out.sizes[dim]]})
    return out.reindex({dim: axis})


def _within(mask: xr.DataArray, dim: str, days: int) -> xr.DataArray:
    """True if ``mask`` is True on any of the next ``days`` labels.

    One True decides it; otherwise False needs every label decided (else NaN).
    """
    if days < 1:
        raise UsageError(f"within window must be >= 1d; got {days}")
    shifted = [mask.shift({dim: -k}) for k in range(1, days + 1)]
    stack = xr.concat(shifted, dim="_look")
    complete = stack.notnull().all("_look")
    hit = (stack == 1).any("_look")
    return xr.where(hit, 1.0, xr.where(complete, 0.0, np.nan))


def _combine(a: xr.DataArray, b: xr.DataArray, how: str) -> xr.DataArray:
    a, b = xr.align(a, b, join="outer")
    if how == "and":
        both_true = (a == 1) & (b == 1)
        any_false = (a == 0) | (b == 0)
        return xr.where(both_true, 1.0, xr.where(any_false, 0.0, np.nan))
    if how == "or":
        any_true = (a == 1) | (b == 1)
        both_false = (a == 0) & (b == 0)
        return xr.where(any_true, 1.0, xr.where(both_false, 0.0, np.nan))
    raise UsageError(f"unsupported combinator {how!r}")


def _cumulative(mask: xr.DataArray, dim: str) -> xr.DataArray:
    seen = mask.fillna(0).cumsum(dim) > 0
    return xr.where(seen, 1.0, mask).astype("float32")


def _detect_any(mask: xr.DataArray, dim: str) -> xr.DataArray:
    hit = (mask == 1).any(dim)
    # A gap followed by decided days could hide the event: unknown, not 0. The
    # trailing NaN run is the undecidable look-ahead tail, reported on stderr.
    decided = mask.notnull()
    gap = (~decided & (decided.cumsum(dim) < decided.sum(dim))).any(dim)
    known_miss = decided.any(dim) & ~gap
    return xr.where(hit, 1.0, xr.where(known_miss, 0.0, np.nan))


def _detect_first(mask: xr.DataArray, dim: str) -> xr.Dataset:
    filled = mask.fillna(0)
    any_true = (mask == 1).any(dim)
    # A gap before the first True could hide an earlier onset: unknown, not later.
    gap_before = (mask.isnull() & (filled.cumsum(dim) == 0)).any(dim)
    known = any_true & ~gap_before
    first = filled.idxmax(dim).where(known)
    first.name = "indicator_time"
    out = first.to_dataset()
    if np.issubdtype(mask[dim].dtype, np.datetime64):
        doy = first.dt.dayofyear.astype("float32")
        out["indicator_doy"] = doy.where(known)
    return out


def _probability(mask: xr.DataArray) -> xr.DataArray:
    if "number" not in mask.dims:
        return mask.astype("float32")
    # NaN unless every member is decided: skipping undecided members biases the
    # fraction (with --cumulative, only members that already hit stay decided).
    return mask.mean("number", skipna=False).astype("float32")


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
    """Apply a yes/no weather rule to each day or week of a Zarr, optionally reduced to an onset date or a probability."""
    spec = parse_rule(rule)
    dim = _axis(ds, time_dim)
    step_days = _step_days(ds, dim)
    # Thresholds are in standard units: air temperature in degree_Celsius, precip
    # in mm per step (a rate in any units times the step; daily mm day-1 is unchanged).
    names = sorted({variable or c.variable for c in spec.clauses} & set(ds.data_vars))
    ds = to_standard_units(ds, variables=names)
    for name in names:
        if kind_from_units(variable_units(ds[name]) or "") == "precip":
            ds[name] = rate_to_total(ds[name], f"{step_days} day").pint.dequantify()
    mask = evaluate_spec(ds, spec, dim, variable, step_days)
    out = apply_reductions(
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
    # Forward-looking rules (windows, within/after) cannot be decided at the end
    # of the series: NaN there is "not yet known", not "no event".
    ahead = max(c.window - step_days + (c.after or c.within or 0) for c in spec.clauses)
    if ahead > 0:
        import datetime as _dt

        last = ds[dim].to_index()[-1]  # Timestamp, Timedelta (step), or cftime
        decided = last - _dt.timedelta(days=ahead)
        print(
            f"Note: --rule {spec.source!r} looks {ahead} day(s) past each date, so dates "
            f"after {decided} cannot be decided from data ending {last}: NaN there means "
            "'not yet known', not 'no event'. With --detect first, a missing date means "
            f"no event through {decided} or not yet known. Extend the input (later obs "
            "or a longer forecast) to decide later dates.",
            file=sys.stderr,
        )
    return out


if __name__ == "__main__":
    indicator()

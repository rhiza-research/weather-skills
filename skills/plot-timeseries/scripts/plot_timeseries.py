# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@plotting-refactor",
#   "cf-xarray",
#   "cftime",
#   "matplotlib>=3.8",
#   "seaborn>=0.13",
#   "numpy",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Render a multi-input timeseries PNG from weather-skills standard dataset Zarrs."""

import argparse
import re
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.display_labels import dataset_display_label
from weather_skills_core.plot import export
from weather_skills_core.plot.charts import compile_lines, leftover_dims
from weather_skills_core.plot.figure import (
    DEFAULT_FONTSIZE,
    format_plot_date,
    is_datetime_axis,
    parse_figsize,  # noqa: F401 — tests call this via the skill module
    resolve_axis_label,
    resolve_time_axis_label,
)
from weather_skills_core.plot.spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    SPEC_VERSION,
    datasets_from_cli_or_spec,
    maybe_emit_spec,
    normalize_spec,
    overlay_spec,
    params_from_spec,
    parse_plot_spec,
    spec_inputs_from_datasets,
    trace_at,
)
from weather_skills_core.plot.theme import (
    along_dim,
    along_member_label,
    normalize_template,
    parse_along_color,
    parse_band,
)
from weather_skills_core.standard_utils import pick_time_dim
from weather_skills_core.units import (
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_label_for_display,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_is_datetime_axis = is_datetime_axis
_resolve_axis_label = resolve_axis_label
_resolve_time_axis_label = resolve_time_axis_label


def _size1_str(ds, *names) -> str | None:
    for name in names:
        if name not in ds.coords and name not in getattr(ds, "variables", ()):
            continue
        arr = ds[name]
        if arr.size != 1:
            continue
        text = str(arr.values.reshape(-1)[0]).strip()
        if text:
            return text
    return None


def _source_stem(ds) -> str | None:
    source = ds.encoding.get("source")
    if isinstance(source, str) and source.strip():
        stem = Path(source).stem
        if stem:
            return stem
    return None


_TRACE_KEYS = {
    "color": "color",
    "linewidth": "linewidth",
    "lw": "linewidth",
    "linestyle": "linestyle",
    "ls": "linestyle",
    "marker": "marker",
    "markersize": "markersize",
    "ms": "markersize",
    "alpha": "alpha",
    "zorder": "zorder",
    "mark": "mark",
}
_LINE_ONLY_KEYS = frozenset({"linewidth", "linestyle", "marker", "markersize"})
_BAR_KEYS = frozenset({"color", "alpha", "zorder"})
_TRACE_STYLES = frozenset({"line", "bar"})
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z]+")


class TraceSpec:
    """One ``--trace SELECTOR:k=v[,k=v...]`` entry."""

    def __init__(self, selector, options, raw):
        self.selector = selector
        self.options = options
        self.raw = raw

    def __str__(self):
        return self.raw

    def __repr__(self):
        return f"TraceSpec({self.raw!r})"


def _parse_trace_options(blob: str) -> dict:
    """Parse ``k=v,k=v`` into canonical matplotlib kwargs."""
    if not blob.strip():
        raise ValueError("--trace needs at least one k=v option (e.g. color=black)")
    options = {}
    for token in blob.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(
                f"--trace option {token!r} is not k=v; expected color=, linewidth=, ..."
            )
        key, _, val = token.partition("=")
        key, val = key.strip(), val.strip()
        if not key:
            raise ValueError(f"--trace option {token!r} has an empty key")
        canon = _TRACE_KEYS.get(key)
        if canon is None:
            raise ValueError(
                f"unknown --trace option {key!r}; expected one of "
                f"{', '.join(sorted(set(_TRACE_KEYS.values())))}"
            )
        if not val:
            raise ValueError(f"--trace option {key!r} has an empty value")
        if canon in options:
            raise ValueError(f"--trace option {key!r} is given more than once")
        if canon == "mark":
            kind = val.casefold()
            if kind not in _TRACE_STYLES:
                raise ValueError(f"--trace mark={val!r} must be line or bar")
            options[canon] = kind
        elif canon in {"linewidth", "markersize", "alpha", "zorder"}:
            try:
                num = float(val)
            except ValueError as exc:
                raise ValueError(f"--trace {key}={val!r} is not a number") from exc
            if canon == "alpha" and not 0.0 <= num <= 1.0:
                raise ValueError(f"--trace alpha={val!r} must be between 0 and 1")
            options[canon] = num
        else:
            options[canon] = val
    if not options:
        raise ValueError("--trace needs at least one k=v option (e.g. color=black)")
    return options


def parse_trace(value) -> TraceSpec:
    """Argparse converter for ``SELECTOR:k=v[,k=v...]``."""
    if not value or not str(value).strip():
        raise argparse.ArgumentTypeError("--trace spec is empty")
    raw = str(value).strip()
    if ":" not in raw:
        raise argparse.ArgumentTypeError(
            f"--trace {raw!r} must be SELECTOR:k=v (e.g. 2026:color=black,linewidth=2.5)"
        )
    selector, _, blob = raw.partition(":")
    selector = selector.strip()
    if not selector:
        raise argparse.ArgumentTypeError(f"--trace {raw!r} is missing a selector")
    try:
        options = _parse_trace_options(blob)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return TraceSpec(selector, options, raw)


def _label_tokens(label: str) -> set[str]:
    return {part.casefold() for part in _TOKEN_SPLIT.split(label) if part}


def _trace_match_indices(selector: str, labels: list[str]) -> list[int]:
    """1-based index, exact legend label, or a unique alphanumeric token in the label."""
    if selector == "*":
        return list(range(len(labels)))
    if selector.isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(labels):
            return [idx]
    folded = selector.casefold()
    exact = [i for i, label in enumerate(labels) if label.casefold() == folded]
    if exact:
        return exact
    return [i for i, label in enumerate(labels) if folded in _label_tokens(label)]


def resolve_trace_styles(labels: list[str], specs: list[TraceSpec] | None) -> list[dict]:
    """Merge ``--trace`` specs onto one style dict per series (``*`` first, then specific)."""
    styles = [{} for _ in labels]
    if not specs:
        return styles
    wildcards = [spec for spec in specs if spec.selector == "*"]
    specific = [spec for spec in specs if spec.selector != "*"]
    for spec in wildcards:
        for style in styles:
            style.update(spec.options)
    for spec in specific:
        hits = _trace_match_indices(spec.selector, labels)
        if not hits:
            raise UsageError(
                f"--trace {spec.raw!r} matched no series. Selectors are a 1-based --input "
                f"index, a legend label, a unique token in a label (e.g. 2026), or *. "
                f"Legend labels: {labels}."
            )
        if len(hits) > 1:
            matched = [labels[i] for i in hits]
            raise UsageError(
                f"--trace {spec.raw!r} matched more than one series ({matched}). "
                "Use a 1-based --input index or a more specific label."
            )
        styles[hits[0]].update(spec.options)
    return styles


def _is_color_like(color) -> bool:
    if isinstance(color, (int, float)):
        return 0.0 <= float(color) <= 1.0
    raw = str(color).strip()
    if not raw:
        return False
    if raw.startswith("#"):
        h = raw[1:]
        return len(h) in (3, 4, 6, 8) and all(c in "0123456789abcdefABCDEF" for c in h)
    try:
        v = float(raw)
    except ValueError:
        return raw.replace(" ", "").replace("-", "").isalpha()
    return 0.0 <= v <= 1.0


def _validate_trace_colors(styles: list[dict]) -> None:
    for style in styles:
        color = style.get("color")
        if color is not None and not _is_color_like(color):
            raise UsageError(
                f"--trace color={color!r} is not a matplotlib color (name, hex, or grayscale 0-1)."
            )


def _series_kind(style: dict, default: str, yvals=None) -> str:
    import numpy as np

    if yvals is not None and np.asarray(yvals).ndim == 2:
        if style.get("mark") == "bar":
            raise UsageError(
                "traces[].along series are drawn as lines; do not set mark bar on an along series."
            )
        return "line"
    kind = style.get("mark", default)
    if kind not in _TRACE_STYLES:
        raise UsageError(f"traces[].mark {kind!r} must be line or bar")
    return kind


def _along_dim(da, along: str | None) -> str | None:
    """Resolve ``traces[].along`` to a dim on ``da``, including ontology aliases."""
    return along_dim(da, along)


def _bar_kwargs(style: dict) -> dict:
    extra = [k for k in style if k in _LINE_ONLY_KEYS]
    if extra:
        raise UsageError(
            f"traces[].line keys {sorted(extra)} apply to line traces, not bar traces. "
            "Use color, alpha, or zorder, or set mark to line on this series."
        )
    return {k: v for k, v in style.items() if k in _BAR_KEYS}


def _as_name_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _series_variable(inputs, idx, ds) -> str | None:
    """``inputs[idx].variable``, else ``inputs[0].variable``, else auto-detect."""
    items = [item for item in (inputs or []) if isinstance(item, dict)]
    own = items[idx].get("variable") if idx < len(items) else None
    if own:
        return own
    fallback = items[0].get("variable") if items else None
    if fallback:
        return fallback
    return auto_variable(ds)


def _series_reduce(traces, idx) -> list:
    """``traces[idx].reduce`` when set, otherwise ``traces[0].reduce``."""
    items = [item for item in (traces or []) if isinstance(item, dict)]
    if idx < len(items) and items[idx].get("reduce") is not None:
        return _as_name_list(items[idx].get("reduce"))
    base = items[0] if items else {}
    return _as_name_list(base.get("reduce"))


def _trace_label(ds, idx: int, override: str | None = None) -> str:
    """Legend label: explicit --label, station id, filename stem, else provenance."""
    if override:
        return override
    station = _size1_str(ds, "station_id", "point_id")
    if station:
        name = _size1_str(ds, "name")
        if name and name.casefold() != station.casefold():
            return f"{station} {name}"
        return station
    stem = _source_stem(ds)
    if stem:
        return stem
    return dataset_display_label(ds, f"input {idx + 1}")


def _y_label(variable, da):
    return variable_label_for_display(da, fallback=variable)


def _day_of_year_tick_label(doy: float) -> str:
    """Map a 1-based day-of-year tick value to a short calendar label."""
    import datetime as dt

    day = int(round(doy))
    if day < 1 or day > 366:
        return ""
    if day == 366:
        return format_plot_date(dt.date(2023, 12, 31), year=False)
    date = dt.date(2023, 1, 1) + dt.timedelta(days=day - 1)
    return format_plot_date(date, year=False)


def _styles_from_traces(traces, n):
    """Per-series style from ``traces[].line`` / ``bar`` / ``mark``."""
    styles = []
    for i in range(n):
        tr = traces[i] if i < len(traces) and isinstance(traces[i], dict) else {}
        block = {}
        for key in ("line", "bar"):
            artist = tr.get(key)
            if isinstance(artist, dict):
                block.update(artist)
        if tr.get("mark"):
            block["mark"] = tr["mark"]
        styles.append(block)
    return styles


@weather_skill(
    name="plot-timeseries",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=False)
@weather_skill.argument(
    "--spec",
    default=None,
    type=parse_plot_spec,
    help=SPEC_ARGUMENT_HELP,
)
@weather_skill.argument(
    "--dump-spec",
    nargs="?",
    const="-",
    default=None,
    probe=True,
    help=DUMP_SPEC_ARGUMENT_HELP,
)
def plot_timeseries(
    ds,
    output,
    spec=None,
    dump_spec=None,
    **kwargs,
):
    """Render a multi-input timeseries PNG from weather-skills standard dataset Zarrs."""
    datasets = datasets_from_cli_or_spec(ds, spec)
    user = spec.to_dict() if spec is not None else {}
    if len(datasets) > 26:
        raise UsageError(f"--input must be passed at most 26 times; got {len(datasets)}.")
    named = {chr(ord("a") + i): d for i, d in enumerate(datasets)}
    internal = {
        "version": SPEC_VERSION,
        "skill": "plot-timeseries",
        "inputs": spec_inputs_from_datasets(named),
        "traces": [
            {"kind": "timeseries", "input": key, "mark": "line"} for key in named
        ],
        "layout": {"bar_mode": "grouped"},
        "axes": {},
        "theme": {"template": "weather_skills"},
    }
    spec_data = normalize_spec(overlay_spec(internal, user))
    trace0 = trace_at(spec_data)
    params = params_from_spec(spec_data)
    title, xlabel, ylabel = params["title"], params["xlabel"], params["ylabel"]
    along = trace0.get("along")
    along_color = parse_along_color(trace0.get("along_color"))
    figsize = tuple(params["figsize"]) if params["figsize"] else None
    mark = trace0.get("mark") or "line"
    per_trace = bool((spec_data.get("layout") or {}).get("facet", {}).get("per_trace"))
    align_day_of_year = trace0.get("align") in (
        True,
        "dayofyear",
        "day-of-year",
        "day_of_year",
    )
    band = trace0.get("band")
    theme = (spec_data.get("theme") or {}).get("template") or "weather_skills"
    fontsize = (spec_data.get("theme") or {}).get("fontsize") or DEFAULT_FONTSIZE
    time_dim = trace0.get("time_dim")
    label_slots = [
        (item.get("label") if isinstance(item, dict) else None)
        for item in (spec_data.get("inputs") or [])
    ]
    while len(label_slots) < len(datasets):
        label_slots.append(None)
    label = label_slots

    if maybe_emit_spec(spec_data, dump_spec, datasets=named):
        return None
    if output is None:
        raise UsageError("--output is required unless --dump-spec is set")

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np

    inputs = [item for item in (spec_data.get("inputs") or []) if isinstance(item, dict)]
    traces = spec_data.get("traces") or []
    variables = [_series_variable(inputs, idx, dataset) for idx, dataset in enumerate(datasets)]
    for idx, (dataset, variable) in enumerate(zip(datasets, variables, strict=True)):
        if not variable or variable not in dataset:
            raise UsageError(
                f"variable {variable!r} missing from input {idx + 1}. "
                f"Available: {list(dataset.data_vars)}. Set inputs[{idx}].variable."
            )
    datasets = [
        precip_for_display(to_standard_units(dataset, variables=[variable]), variable)
        for dataset, variable in zip(datasets, variables, strict=True)
    ]

    unit_vals = []
    seen_units = {}
    for idx, (dataset, variable) in enumerate(zip(datasets, variables, strict=True)):
        u = variable_units(dataset[variable])
        if isinstance(u, str) and u.strip():
            unit_vals.append(u)
            seen_units[_trace_label(dataset, idx, label_slots[idx])] = u.strip()
    if not per_trace and unit_vals and any(not units_equal(unit_vals[0], u) for u in unit_vals[1:]):
        detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
        plotted = variables[0] if len(set(variables)) == 1 else ", ".join(variables)
        print(
            f"Warning: variable '{plotted}' has differing units across the "
            f"overlaid inputs ({detail}). The series share one y-axis labeled "
            f"with a single unit, so values in different units are not directly "
            f"comparable in this figure. Set layout.facet.per_trace to give each "
            f"input its own y-axis.",
            file=sys.stderr,
        )

    y_labels = [
        _y_label(variable, dataset[variable])
        for dataset, variable in zip(datasets, variables, strict=True)
    ]
    first_tdim = None
    axis_label = None
    series = []
    along_labels_by_series = []
    band_q = parse_band(band)
    if band_q is not None and not along:
        raise UsageError(
            "traces[].band requires traces[].along (percentiles are taken over that dim)."
        )
    if trace0.get("along_color") and not along:
        raise UsageError("traces[].along_color requires traces[].along.")
    if along_color == "cycle" and band_q is not None:
        raise UsageError("traces[].along_color cycle cannot be combined with traces[].band.")
    template = normalize_template(theme)

    for idx, (dataset, variable) in enumerate(zip(datasets, variables, strict=True)):
        da = dataset[variable]
        try:
            tdim = pick_time_dim(da, time_dim)
        except UsageError as exc:
            raise UsageError(f"Error (input {idx + 1}): {exc}", prefix=False) from None

        reduce = _series_reduce(traces, idx)
        applicable = [d for d in reduce if d in da.dims]
        if applicable:
            da = da.mean(applicable, keep_attrs=True)

        along_resolved = _along_dim(da, along)
        if along_resolved == tdim:
            raise UsageError(
                f"Error (input {idx + 1}): traces[].along {along!r} is the time axis "
                f"('{tdim}'); set a non-time dim such as number.",
                prefix=False,
            )
        extras = leftover_dims(da, tdim, along=along_resolved)
        if along and along_resolved is None and extras:
            raise UsageError(
                f"Error (input {idx + 1}): traces[].along {along!r} is not a dim of "
                f"variable '{variable}' (dims: {list(da.dims)}).",
                prefix=False,
            )
        if extras:
            hint = extras[0]
            raise UsageError(
                f"Error (input {idx + 1}): variable '{variable}' still has non-time dims "
                f"{extras} after traces[].reduce. Set traces[].reduce for each, or "
                f"traces[].along {hint!r} to draw one line per {hint} value.",
                prefix=False,
            )

        label = _trace_label(dataset, idx, label_slots[idx])
        series_xlabel = tdim
        if align_day_of_year:
            try:
                xvals = da[tdim].dt.dayofyear.values
            except (TypeError, AttributeError):
                raise UsageError(
                    f"Error (input {idx + 1}): traces[].align dayofyear needs a calendar-date "
                    f"time axis, but '{tdim}' is not a date axis. Drop align or pick "
                    f"a date dim with traces[].time_dim.",
                    prefix=False,
                ) from None
            if len(xvals) > 1 and np.any(np.diff(xvals) < 0):
                print(
                    f"Warning (input {idx + 1}): day-of-year values are non-monotonic; "
                    f"rendering anyway.",
                    file=sys.stderr,
                )
            series_xlabel = "calendar day"
        else:
            xvals = da[tdim].values
            if (
                tdim == "step"
                and np.issubdtype(np.asarray(xvals).dtype, np.timedelta64)
                and "time" in dataset.coords
                and dataset["time"].ndim == 0
                and np.asarray(dataset["time"].values).dtype.kind == "M"
            ):
                xvals = (np.asarray(dataset["time"].values) + np.asarray(xvals)).astype(
                    "datetime64[ns]"
                )
                series_xlabel = "valid time"
        if along_resolved:
            da = da.transpose(tdim, along_resolved)
            along_labels_by_series.append(
                [along_member_label(v) for v in da[along_resolved].values]
            )
        else:
            along_labels_by_series.append(None)
        series.append((xvals, np.asarray(da.values), label))

        if first_tdim is None:
            first_tdim = tdim
        if axis_label is None:
            axis_label = series_xlabel

    styles = _styles_from_traces(spec_data.get("traces") or [], len(series))
    _validate_trace_colors(styles)
    for series_style, (_, yvals, _), member_labels in zip(
        styles, series, along_labels_by_series, strict=True
    ):
        if member_labels:
            series_style["along_labels"] = member_labels
        if along and np.asarray(yvals).ndim == 2:
            series_style["along_color"] = along_color
    if band_q is not None:
        for series_style, (_, yvals, _) in zip(styles, series, strict=True):
            if np.asarray(yvals).ndim == 2:
                if series_style.get("mark") == "bar":
                    raise UsageError("traces[].band is not supported on bar traces; set mark to line.")
                series_style["band"] = band_q
    x_for_label = series[0][0] if series else None
    resolved_xlabel = _resolve_time_axis_label(
        xlabel, axis_label or first_tdim or "time", x_for_label
    )
    kinds = [
        _series_kind(series_style, mark, yvals)
        for (_, yvals, _), series_style in zip(series, styles, strict=True)
    ]
    for kind, series_style in zip(kinds, styles, strict=True):
        if kind == "bar":
            _bar_kwargs(series_style)
    panel_ylabels = (
        [_resolve_axis_label(ylabel, lab) for lab in y_labels]
        if per_trace
        else [_resolve_axis_label(ylabel, y_labels[0])]
    )
    if align_day_of_year:
        if not isinstance(spec_data.get("axes"), dict):
            spec_data["axes"] = {}
        spec_data["axes"].setdefault("xformatter", "dayofyear")
    compiled = compile_lines(
        series,
        title=title,
        xlabel=resolved_xlabel,
        ylabels=panel_ylabels,
        fontsize=fontsize,
        figsize=figsize,
        per_trace=per_trace,
        kinds=kinds,
        styles=styles,
        template=template,
        spec=spec_data,
    )
    spec_data["xlabel"] = resolved_xlabel
    compiled.spec = spec_data
    return export(
        compiled,
        output,
        datasets=named,
        spec=spec_data,
    )


if __name__ == "__main__":
    plot_timeseries()

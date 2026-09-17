# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@plot-refactor",
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
from weather_skills_core.display_labels import dataset_display_label, resolve_input_labels
from weather_skills_core.figure import (
    DEFAULT_FONTSIZE,
    format_plot_date,
    is_datetime_axis,
    parse_figsize,
    resolve_axis_label,
    resolve_time_axis_label,
)
from weather_skills_core.plot_spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    datasets_from_cli_or_spec,
    dump_spec_dest,
    parse_plot_spec,
    spec_input_labels,
    spec_inputs_from_datasets,
)
from weather_skills_core.plot_style import along_dim, normalize_template, parse_band
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
    "style": "style",
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
        if canon == "style":
            kind = val.casefold()
            if kind not in _TRACE_STYLES:
                raise ValueError(f"--trace style={val!r} must be line or bar")
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
        if style.get("style") == "bar":
            raise UsageError(
                "--along traces are drawn as lines; do not set style=bar on an --along series."
            )
        return "line"
    kind = style.get("style", default)
    if kind not in _TRACE_STYLES:
        raise UsageError(f"--trace style={kind!r} must be line or bar")
    return kind


def _along_dim(da, along: str | None) -> str | None:
    """Resolve ``--along`` to a dim on ``da``, including ontology aliases (member/number)."""
    return along_dim(da, along)


def _bar_kwargs(style: dict) -> dict:
    extra = [k for k in style if k in _LINE_ONLY_KEYS]
    if extra:
        raise UsageError(
            f"--trace keys {sorted(extra)} apply to line traces, not bar traces. "
            "Use color, alpha, or zorder, or set style=line on this series."
        )
    return {k: v for k, v in style.items() if k in _BAR_KEYS}


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


@weather_skill(
    name="plot-timeseries",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=False)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--time-dim",
    default=None,
    help="Time-like dim; default time, then step, then CF time.",
)
@weather_skill.argument(
    "--reduce",
    action="append",
    default=None,
    help="Non-time dim to mean-reduce before plotting. Repeatable.",
)
@weather_skill.argument(
    "--along",
    default=None,
    help=(
        "Non-time dim to fan into traces (e.g. number/member). One input, "
        "one legend entry; not one --input per member."
    ),
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--xlabel",
    default=None,
    help="Override the x-axis label (default: omitted on datetime ticks; else Calendar day / Step).",
)
@weather_skill.argument(
    "--ylabel",
    default=None,
    help="Override the y-axis label (default: from variable metadata).",
)
@weather_skill.argument(
    "--fontsize",
    type=int,
    default=DEFAULT_FONTSIZE,
    help="Base font size for titles, axis labels, ticks, and legend (default 16).",
)
@weather_skill.argument(
    "--figsize",
    default=None,
    type=parse_figsize,
    help="Figure size W,H inches (e.g. 10,6 or 10x6). Default 10,6.",
)
@weather_skill.argument(
    "--style",
    choices=["line", "bar"],
    default=None,
    help="line (default) or grouped bar.",
)
@weather_skill.argument(
    "--align-day-of-year",
    action="store_true",
    help="Plot against day-of-year (1-366) instead of absolute date.",
)
@weather_skill.argument(
    "--band",
    default=None,
    help=(
        "Ensemble envelope along --along: two percentiles, e.g. 10,90 (default when "
        "the flag is passed as --band with no value: 10,90). Draws a filled range plus "
        "the mean instead of spaghetti members."
    ),
)
@weather_skill.argument(
    "--theme",
    default=None,
    choices=["weather_skills", "colorblind"],
    help="Seaborn colorway: weather_skills (deep) or colorblind.",
)
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help=(
        "Legend label (overlay) or subplot title (--subplots) for each --input, "
        "in order. Omit to infer from metadata."
    ),
)
@weather_skill.argument(
    "--trace",
    action="append",
    default=[],
    type=parse_trace,
    help=(
        "Per-series style SELECTOR:k=v. Repeatable. Selector is a 1-based "
        "--input index (1..N), legend label, unique token in the label "
        "(e.g. 2026), or * for all. Keys: color, linewidth, linestyle, "
        "marker, markersize, alpha, zorder, style (line|bar, overrides "
        "global --style for that series)."
    ),
)
@weather_skill.argument(
    "--subplots",
    action="store_true",
    help=(
        "One stacked subplot per --input (shared time axis, independent y-scales) "
        "instead of overlaying traces on a single axes."
    ),
)
@weather_skill.argument(
    "--spec",
    default=None,
    type=parse_plot_spec,
    help=SPEC_ARGUMENT_HELP,
)
@weather_skill.argument(
    "--dump-spec",
    default=None,
    help=DUMP_SPEC_ARGUMENT_HELP,
)
def plot_timeseries(
    ds,
    variable,
    time_dim,
    reduce,
    along,
    title,
    xlabel,
    ylabel,
    fontsize,
    figsize,
    style,
    align_day_of_year,
    label,
    trace,
    output,
    subplots=False,
    band=None,
    theme=None,
    spec=None,
    dump_spec=None,
    **kwargs,
):
    """Render a multi-input timeseries PNG from weather-skills standard dataset Zarrs."""
    datasets = datasets_from_cli_or_spec(ds, spec)
    spec_data = spec.to_dict() if spec is not None else {}
    traces_spec = spec_data.get("traces") or []
    first_trace = traces_spec[0] if traces_spec else {}
    first_input = (spec_data.get("inputs") or [{}])[0]
    if isinstance(first_input, dict):
        variable = variable or first_input.get("variable")
    else:
        first_input = {}
    if not reduce:
        reduce = first_trace.get("reduce") or []
    along = along or first_trace.get("along")
    title = title if title is not None else spec_data.get("title")
    xlabel = xlabel if xlabel is not None else spec_data.get("xlabel")
    ylabel = ylabel if ylabel is not None else spec_data.get("ylabel")
    layout = spec_data.get("layout") or {}
    style_block = spec_data.get("style") or {}
    if figsize is None and layout.get("figsize"):
        figsize = tuple(layout["figsize"])
    style = style or first_trace.get("style") or "line"
    if not subplots:
        subplots = bool(layout.get("subplots"))
    if not align_day_of_year and spec_data.get("align") in ("dayofyear", "day-of-year"):
        align_day_of_year = True
    if band is None:
        band = spec_data.get("band")
    theme = theme or style_block.get("template") or "weather_skills"
    if not label:
        label = spec_input_labels(spec_data)
    if len(datasets) > 26:
        raise UsageError(f"--input must be passed at most 26 times; got {len(datasets)}.")
    label_slots = resolve_input_labels(label, len(datasets))

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np

    variable = variable or auto_variable(datasets[0])
    if variable is None:
        raise UsageError("no usable variable in the first input.")
    for idx, ds in enumerate(datasets):
        if variable not in ds:
            raise UsageError(
                f"variable '{variable}' missing from input {idx + 1}. "
                f"Available: {list(ds.data_vars)}"
            )
    datasets = [
        precip_for_display(to_standard_units(ds, variables=[variable]), variable) for ds in datasets
    ]

    unit_vals = []
    seen_units = {}
    for idx, ds in enumerate(datasets):
        u = variable_units(ds[variable])
        if isinstance(u, str) and u.strip():
            unit_vals.append(u)
            seen_units[_trace_label(ds, idx, label_slots[idx])] = u.strip()
    if not subplots and unit_vals and any(not units_equal(unit_vals[0], u) for u in unit_vals[1:]):
        detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
        print(
            f"Warning: variable '{variable}' has differing units across the "
            f"overlaid inputs ({detail}). The series share one y-axis labeled "
            f"with a single unit, so values in different units are not directly "
            f"comparable in this figure. Pass --subplots to give each input "
            f"its own y-axis.",
            file=sys.stderr,
        )

    y_labels = [_y_label(variable, ds[variable]) for ds in datasets]
    first_tdim = None
    axis_label = None
    series = []
    band_q = parse_band(band)
    if band_q is not None and not along:
        raise UsageError("--band requires --along (percentiles are taken over that dim).")
    template = normalize_template(theme)

    for idx, ds in enumerate(datasets):
        da = ds[variable]
        try:
            tdim = pick_time_dim(da, time_dim)
        except UsageError as exc:
            raise UsageError(f"Error (input {idx + 1}): {exc}", prefix=False) from None

        applicable = [d for d in reduce if d in da.dims]
        if applicable:
            da = da.mean(applicable, keep_attrs=True)

        extras = [d for d in da.dims if d != tdim]
        along_dim = _along_dim(da, along)
        if along_dim == tdim:
            raise UsageError(
                f"Error (input {idx + 1}): --along {along!r} is the time axis "
                f"('{tdim}'); pass a non-time dim such as number.",
                prefix=False,
            )
        if along and along_dim is None and extras:
            raise UsageError(
                f"Error (input {idx + 1}): --along {along!r} is not a dim of "
                f"variable '{variable}' (dims: {list(da.dims)}).",
                prefix=False,
            )
        if along_dim:
            extras = [d for d in extras if d != along_dim]
        if extras:
            hint = extras[0]
            raise UsageError(
                f"Error (input {idx + 1}): variable '{variable}' still has non-time dims "
                f"{extras} after --reduce. Pass --reduce <dim> for each, or "
                f"--along {hint} to draw one line per {hint} value.",
                prefix=False,
            )

        label = _trace_label(ds, idx, label_slots[idx])
        series_xlabel = tdim
        if align_day_of_year:
            try:
                xvals = da[tdim].dt.dayofyear.values
            except (TypeError, AttributeError):
                raise UsageError(
                    f"Error (input {idx + 1}): --align-day-of-year needs a calendar-date "
                    f"time axis, but '{tdim}' is not a date axis. Drop the flag or pick "
                    f"a date dim with --time-dim.",
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
                and "time" in ds.coords
                and ds["time"].ndim == 0
                and np.asarray(ds["time"].values).dtype.kind == "M"
            ):
                xvals = (np.asarray(ds["time"].values) + np.asarray(xvals)).astype("datetime64[ns]")
                series_xlabel = "valid time"
        if along_dim:
            da = da.transpose(tdim, along_dim)
        series.append((xvals, np.asarray(da.values), label))

        if first_tdim is None:
            first_tdim = tdim
        if axis_label is None:
            axis_label = series_xlabel

    styles = resolve_trace_styles([lab for _, _, lab in series], trace)
    _validate_trace_colors(styles)
    if band_q is not None:
        for series_style, (_, yvals, _) in zip(styles, series, strict=True):
            if np.asarray(yvals).ndim == 2:
                if series_style.get("style") == "bar":
                    raise UsageError("--band is not supported on bar traces; use style=line.")
                series_style["band"] = band_q
    x_for_label = series[0][0] if series else None
    resolved_xlabel = _resolve_time_axis_label(
        xlabel, axis_label or first_tdim or "time", x_for_label
    )
    kinds = [
        _series_kind(series_style, style, yvals)
        for (_, yvals, _), series_style in zip(series, styles, strict=True)
    ]
    for kind, series_style in zip(kinds, styles, strict=True):
        if kind == "bar":
            _bar_kwargs(series_style)
    panel_ylabels = (
        [_resolve_axis_label(ylabel, lab) for lab in y_labels]
        if subplots
        else [_resolve_axis_label(ylabel, y_labels[0])]
    )
    from matplotlib.ticker import FuncFormatter
    from weather_skills_core.plot_export import write_plot_outputs
    from weather_skills_core.plot_recipes import compile_line_figure

    fig = compile_line_figure(
        series,
        title=title,
        xlabel=resolved_xlabel,
        ylabels=panel_ylabels,
        fontsize=fontsize,
        figsize=figsize,
        subplots=subplots,
        kinds=kinds,
        styles=styles,
        template=template,
    )
    if align_day_of_year:
        for ax in fig.axes:
            ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _p: _day_of_year_tick_label(x)))
    named = {chr(ord("a") + i): ds for i, ds in enumerate(datasets)}
    traces = []
    for key in named:
        item = {"type": "timeseries", "input": key, "style": style}
        if along:
            item["along"] = along
        if reduce:
            item["reduce"] = list(reduce)
        traces.append(item)
    inputs = spec_inputs_from_datasets(named)
    for i, item in enumerate(inputs):
        if variable:
            item["variable"] = variable
        if label_slots and i < len(label_slots) and label_slots[i]:
            item["label"] = label_slots[i]
    resolved = {
        "version": 1,
        "skill": "plot-timeseries",
        "inputs": inputs,
        "layout": {"subplots": subplots, "figsize": list(figsize) if figsize else None},
        "traces": traces,
        "style": {"template": template, "fontsize": fontsize},
        "title": title,
        "xlabel": resolved_xlabel,
        "ylabel": ylabel,
    }
    if align_day_of_year:
        resolved["align"] = "dayofyear"
    if band_q is not None:
        resolved["band"] = list(band_q)
    return write_plot_outputs(
        fig, resolved, output, datasets=named, dump_spec_path=dump_spec_dest(dump_spec)
    )


if __name__ == "__main__":
    plot_timeseries()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cartopy",
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: cartopy gridliner crash
#   "matplotlib>=3.8,<3.10",
#   "seaborn>=0.13",
#   "nc-time-axis",
#   "numpy",
#   "shapely>=2.1",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Render a heatmap, timeseries, xy scatter, wind-rose, or quiver PNG from a weather-skills standard dataset Zarr."""

import argparse
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.plot import compile, export
from weather_skills_core.plot.figure import (  # noqa: F401 — tests call these via the skill module
    parse_figsize,
    parse_panel_spacing,
)
from weather_skills_core.plot.maps import parse_draw_boxes, parse_layer
from weather_skills_core.plot.spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    fold_layer_options,
    layer_item_from_parts,
    maybe_emit_spec,
    named_datasets_from_spec,
    normalize_spec,
    opened_datasets_from_spec,
    overlay_spec,
    params_from_spec,
    parse_index,
    parse_plot_spec,
    spec_from_flags,
    spec_inputs_from_datasets,
    trace_at,
)
from weather_skills_core.plot.theme import load_user_theme

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_MAP_KINDS = frozenset({"heatmap", "contour", "quiver"})
_ZARR_LAYER_KINDS = frozenset({"heatmap", "scatter", "quiver"})

_MPL_LEGEND_LOCS = frozenset(
    {
        "best",
        "upper right",
        "upper left",
        "lower left",
        "lower right",
        "right",
        "center left",
        "center right",
        "lower center",
        "upper center",
        "center",
    }
)
_LEGEND_ALIASES = {
    "outside": "outside right",
    "outside right": "outside right",
    "right outside": "outside right",
    "below": "below",
    "bottom": "below",
    "none": "none",
    "off": "none",
}


def parse_legend(value):
    """Argparse converter for a matplotlib loc, ``outside right``, ``below``, or ``none``."""
    if value is None:
        return None
    loc = " ".join(str(value).strip().lower().replace("_", " ").replace("-", " ").split())
    if not loc:
        raise argparse.ArgumentTypeError("--legend placement is empty")
    loc = _LEGEND_ALIASES.get(loc, loc)
    if loc in _MPL_LEGEND_LOCS or loc in ("outside right", "below", "none"):
        return loc
    allowed = ", ".join(sorted(_MPL_LEGEND_LOCS | {"below", "none", "outside right"}))
    raise argparse.ArgumentTypeError(f"--legend {value!r} is not a placement ({allowed})")


def _input_path_of(ds):
    from weather_skills_core.decorator import INPUT_PATH_ATTR

    if ds is None:
        return None
    return ds.attrs.get(INPUT_PATH_ATTR)


def _dataset_list(ds):
    """One Dataset, a list from repeatable ``-i``, or nothing."""
    if ds is None:
        return []
    if isinstance(ds, (list, tuple)):
        return [item for item in ds if item is not None]
    return [ds]


def _letter_datasets(datasets):
    """``a``, ``b``, ``c``, … in the order ``-i`` was passed."""
    named = {}
    for i, item in enumerate(datasets):
        key = chr(ord("a") + i) if i < 26 else f"i{i}"
        named[key] = item
    return named


def _fill_side_by_side_titles(spec):
    """Name each panel when several map traces share one figure.

    ``subplot_titles`` wins. Otherwise a panel uses ``inputs[].label``, then
    the file name. One-trace figures keep the calendar-date title.
    """
    if spec.get("layers") or spec.get("subplot_titles"):
        return
    traces = [t for t in (spec.get("traces") or []) if isinstance(t, dict)]
    if len(traces) < 2 or any(t.get("kind") not in _MAP_KINDS for t in traces):
        return
    inputs = [i for i in (spec.get("inputs") or []) if isinstance(i, dict)]
    if len(inputs) != len(traces):
        return
    titles = []
    for item in inputs:
        if item.get("label"):
            titles.append(str(item["label"]))
        elif item.get("path"):
            titles.append(Path(item["path"]).stem)
        else:
            return
    spec["subplot_titles"] = titles


def _spec_data(spec):
    if spec is None:
        return {}
    if hasattr(spec, "to_dict"):
        return spec.to_dict()
    return dict(spec) if isinstance(spec, dict) else {}


def _kind_from_spec(spec_data):
    if spec_data.get("layers"):
        return None
    kind = trace_at(spec_data).get("kind")
    return kind if kind and kind not in {"layer", "layers", "grid", "mediogram"} else None


def _datasets_by_path(spec):
    from weather_skills_core.decorator import INPUT_PATH_ATTR

    mapping = {}
    for ds in opened_datasets_from_spec(spec):
        raw = getattr(ds, "attrs", {}).get(INPUT_PATH_ATTR)
        if not raw:
            continue
        mapping[str(Path(raw))] = ds
        try:
            mapping[str(Path(raw).resolve())] = ds
        except OSError:
            pass
    return mapping


def _layers_from_spec(spec_data, spec):
    from weather_skills_core.plot.maps import LayerSpec

    raw_layers = spec_data.get("layers") or []
    if not raw_layers:
        return []
    by_path = _datasets_by_path(spec)
    named = named_datasets_from_spec(spec)
    layers = []
    for item in raw_layers:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        path = item.get("path")
        if not kind or not path:
            raise UsageError("spec layers[] entries need kind and path")
        options = fold_layer_options(item)
        raw = item.get("raw") or f"{kind}:{path}"
        layer = LayerSpec(kind, path, options, raw)
        if kind in _ZARR_LAYER_KINDS:
            ds = None
            input_id = item.get("input")
            if input_id:
                ds = named.get(str(input_id))
            if ds is None:
                key = str(Path(path))
                ds = by_path.get(key)
                if ds is None:
                    try:
                        ds = by_path.get(str(Path(path).resolve()))
                    except OSError:
                        ds = None
            layer.ds = ds
        layers.append(layer)
    return layers


def _xy_from_spec(spec_data, spec, x_ds, y_ds, x_variable, y_variable, pair_on):
    named = named_datasets_from_spec(spec)
    opened = opened_datasets_from_spec(spec)
    traces = spec_data.get("traces") or []
    trace = traces[0] if traces and isinstance(traces[0], dict) else {}
    pair_on = pair_on or trace.get("pair_on") or "time"
    inputs = [i for i in (spec_data.get("inputs") or []) if isinstance(i, dict)]
    x_variable = (
        x_variable or trace.get("x_variable") or (inputs[0].get("variable") if inputs else None)
    )
    y_variable = (
        y_variable
        or trace.get("y_variable")
        or (inputs[1].get("variable") if len(inputs) > 1 else None)
    )
    if x_ds is None:
        x_id = trace.get("x") or trace.get("input") or "x"
        x_ds = named.get(str(x_id)) or named.get("a")
        if x_ds is None and opened:
            x_ds = opened[0]
    if y_ds is None:
        if trace.get("y"):
            y_ds = named.get(str(trace.get("y")))
        elif named.get("y") is not None:
            y_ds = named.get("y")
        elif len(opened) > 1:
            y_ds = opened[1]
        elif trace.get("input") or (x_variable and y_variable and len(opened) <= 1 and named):
            y_ds = named.get(str(trace.get("input") or "a")) or x_ds
    return x_ds, y_ds, x_variable, y_variable, pair_on


def _layer_datasets(layers, labels=None):
    named = {}
    for i, layer in enumerate(layers):
        if layer.kind not in _ZARR_LAYER_KINDS or layer.ds is None:
            continue
        key = chr(ord("a") + len(named))
        named[key] = layer.ds
        if labels and i < len(labels) and labels[i]:
            pass
    return named


def _warn(kind, **flags):
    legend = flags.get("legend")
    legend_used = legend is not None and legend != "none"
    if kind in _MAP_KINDS and legend_used:
        print(
            f"Warning: legend is ignored for traces[0].kind {kind} (maps use a colorbar).",
            file=sys.stderr,
        )
    if kind == "heatmap" and (flags.get("u_variable") or flags.get("v_variable")):
        print(
            "Warning: traces[].u_variable / v_variable are only used with kind windrose or "
            "quiver; ignored for kind heatmap.",
            file=sys.stderr,
        )
    if kind == "timeseries":
        for name, set_ in {
            "geo.extent": bool(flags.get("extent")),
            "geo.cities": bool(flags.get("cities")),
            "geo.draw_boxes": bool(flags.get("draw_boxes")),
            "layout.facet.rows": flags.get("rows") is not None,
            "layout.facet.columns": flags.get("columns") is not None,
            "layout.facet.wspace": flags.get("panel_spacing") is not None,
            "subplot_titles": bool(flags.get("subplot_title")),
            "geo.bbox": flags.get("bbox") is not None,
            "geo.mask_geojson": bool(flags.get("mask_geojson")),
            "inputs[].index": bool(flags.get("index")),
        }.items():
            if set_:
                print(
                    f"Warning: {name} is a map-only spec key; ignored for traces[0].kind {kind}.",
                    file=sys.stderr,
                )
    if kind in {"xy", "windrose"}:
        extras = {
            "geo.extent": bool(flags.get("extent")),
            "geo.cities": bool(flags.get("cities")),
            "geo.draw_boxes": bool(flags.get("draw_boxes")),
            "layout.facet.rows": flags.get("rows") is not None,
            "layout.facet.columns": flags.get("columns") is not None,
            "layout.facet.wspace": flags.get("panel_spacing") is not None,
            "subplot_titles": bool(flags.get("subplot_title")),
        }
        if kind == "xy":
            extras.update(
                {
                    "traces[].u_variable": bool(flags.get("u_variable")),
                    "traces[].v_variable": bool(flags.get("v_variable")),
                    "quiver.scale": flags.get("quiver_scale") is not None,
                    "quiver.step": flags.get("quiver_step") is not None,
                    "vmin": flags.get("vmin") is not None,
                    "vmax": flags.get("vmax") is not None,
                    "cbar_label": bool(flags.get("cbar_label")),
                }
            )
            if flags.get("variable"):
                print(
                    "Warning: inputs[].variable is ignored for kind xy; "
                    "set traces[0].x_variable and traces[0].y_variable.",
                    file=sys.stderr,
                )
            if legend_used:
                print("Warning: legend is ignored for kind xy.", file=sys.stderr)
        if kind == "windrose":
            extras.update(
                {
                    "quiver.scale": flags.get("quiver_scale") is not None,
                    "quiver.step": flags.get("quiver_step") is not None,
                    "vmin": flags.get("vmin") is not None,
                    "vmax": flags.get("vmax") is not None,
                    "cbar_label": bool(flags.get("cbar_label")),
                }
            )
        for name, set_ in extras.items():
            if set_:
                print(
                    f"Warning: {name} is ignored for traces[0].kind {kind}.",
                    file=sys.stderr,
                )


def _internal_from_files(*, kind, datasets, user_theme, layers=None):
    """Skeleton spec from opened files. ``--spec`` is merged on afterwards."""
    flags = {}
    if user_theme.get("template"):
        flags["template"] = user_theme["template"]
    if user_theme.get("colormap"):
        flags["colormap"] = user_theme["colormap"]
    if user_theme.get("fontsize") is not None:
        flags["fontsize"] = user_theme["fontsize"]
    merged = spec_from_flags(kind=kind or "heatmap", **flags)
    if layers:
        named = _layer_datasets(layers)
        merged["traces"] = [{"kind": "layer", "input": "a"}]
        built = []
        inputs = []
        for i, layer in enumerate(layers):
            lid = chr(ord("a") + i)
            input_id = lid if layer.kind in _ZARR_LAYER_KINDS else None
            entry = layer_item_from_parts(
                layer.kind,
                layer.path,
                layer.options,
                layer_id=lid,
                raw=layer.raw,
                input_id=input_id,
            )
            if layer.kind in _ZARR_LAYER_KINDS:
                if layer.ds is not None:
                    named.setdefault(lid, layer.ds)
                inputs.append({"id": lid, "path": str(layer.path)})
            built.append(entry)
        merged["layers"] = built
        if inputs:
            merged["inputs"] = inputs
        datasets = named or datasets
    elif datasets:
        merged["inputs"] = spec_inputs_from_datasets(datasets)
        keys = list(datasets)
        if kind in _MAP_KINDS and len(keys) > 1:
            merged["traces"] = [{"kind": kind, "input": key} for key in keys]
        elif keys and merged.get("traces"):
            merged["traces"][0]["input"] = keys[0]
    if user_theme.get("max_columns"):
        merged.setdefault("layout", {}).setdefault("facet", {}).setdefault(
            "max_columns", user_theme["max_columns"]
        )
    merged["skill"] = "plot"
    return merged, datasets


@weather_skill(
    name="plot",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "-i",
    "--input",
    type=Dataset("any"),
    action="append",
    required=False,
    help=(
        "Input Zarr. Repeat for one map panel per file "
        "(heatmap, contour, or quiver). Mutually exclusive with --layer and with --x/--y."
    ),
)
@weather_skill.argument(
    "--x",
    dest="x_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="X-axis Zarr for kind xy. Mutually exclusive with -i/--input and --layer.",
)
@weather_skill.argument(
    "--y",
    dest="y_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="Y-axis Zarr for kind xy. Mutually exclusive with -i/--input and --layer.",
)
@weather_skill.argument(
    "--layer",
    action="append",
    default=None,
    type=parse_layer,
    help=(
        "Map layer KIND:PATH. Repeat for overlays. "
        "Kinds: heatmap, scatter, quiver, outline, mask. "
        "Set variable, colormap, vmin, and other layer keys on layers[] in --spec. "
        "Mutually exclusive with -i/--input."
    ),
)
@weather_skill.argument(
    "--spec",
    default=None,
    type=parse_plot_spec,
    help=SPEC_ARGUMENT_HELP,
)
@weather_skill.argument(
    "--theme-file",
    default=None,
    help="User plot theme TOML/JSON (colormap, fontsize, template). Overrides ~/.config/weather-skills/theme.toml.",
)
@weather_skill.argument(
    "--dump-spec",
    nargs="?",
    const="-",
    default=None,
    probe=True,
    help=DUMP_SPEC_ARGUMENT_HELP,
)
def plot(
    ds,
    output,
    layer=None,
    x_ds=None,
    y_ds=None,
    spec=None,
    theme_file=None,
    dump_spec=None,
    **kwargs,
):
    """Render a heatmap, contour, timeseries, xy scatter, wind-rose, quiver, or layered map PNG from weather-skills Zarrs."""
    user = _spec_data(spec)
    layers = list(layer or [])
    if not layers:
        layers = _layers_from_spec(user, spec)
    kind = _kind_from_spec(user)
    if kind is None and not layers:
        kind = "heatmap"
    files = _dataset_list(ds)
    if not files and spec is not None and not layers and kind != "xy":
        files = opened_datasets_from_spec(spec)
    if layers and files:
        raise UsageError("pass either -i/--input or --layer, not both")
    if layers and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either --layer or --x/--y, not both")
    if files and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either -i/--input or --x/--y, not both")
    if kind == "xy":
        x_ds, y_ds, _x_variable, _y_variable, _pair_on = _xy_from_spec(
            user, spec, x_ds, y_ds, None, None, None
        )
        if layers:
            raise UsageError("--layer cannot be used with kind xy")
        if x_ds is None and y_ds is None:
            if len(files) != 1:
                raise UsageError(
                    "kind xy needs --x and --y, or one -i with x_variable and y_variable in --spec"
                )
            trace = (user.get("traces") or [{}])[0] if isinstance((user.get("traces") or [{}])[0], dict) else {}
            inputs = [i for i in (user.get("inputs") or []) if isinstance(i, dict)]
            x_variable = trace.get("x_variable") or (inputs[0].get("variable") if inputs else None)
            y_variable = trace.get("y_variable") or (inputs[1].get("variable") if len(inputs) > 1 else None)
            if not x_variable or not y_variable:
                raise UsageError("with a single -i, kind xy needs x_variable and y_variable in --spec")
            x_ds = files[0]
            y_ds = files[0]
        elif x_ds is None or y_ds is None:
            raise UsageError("kind xy needs both --x and --y")
    elif not layers and not files:
        raise UsageError("pass -i/--input, a --spec with inputs, or at least one --layer")
    if not layers and kind in {"timeseries", "windrose"} and len(files) > 1:
        raise UsageError(
            f"kind {kind} takes one -i; repeat -i for a side-by-side heatmap, contour, or quiver"
        )
    if layers and kind in ("timeseries", "xy", "windrose", "contour"):
        raise UsageError(f"--layer cannot be used with kind {kind}")
    if layers and kind == "quiver":
        raise UsageError("with --layer, draw wind vectors as --layer quiver:PATH instead of kind quiver")

    user_theme = load_user_theme(theme_file)
    if kind == "xy":
        datasets = {"a": x_ds} if x_ds is y_ds else {"x": x_ds, "y": y_ds}
    elif layers:
        datasets = {}
    elif not _dataset_list(ds) and spec is not None:
        datasets = named_datasets_from_spec(spec) or _letter_datasets(files)
    else:
        datasets = _letter_datasets(files)

    internal, datasets = _internal_from_files(
        kind="layer" if layers else kind,
        datasets=datasets,
        user_theme=user_theme,
        layers=layers or None,
    )
    merged = normalize_spec(overlay_spec(internal, user))
    _fill_side_by_side_titles(merged)
    filled = params_from_spec(merged)
    kind_now = "layer" if merged.get("layers") else (trace_at(merged).get("kind") or kind)
    parse_index(filled.get("index"))
    draw_boxes = parse_draw_boxes(filled.get("draw_boxes"))
    _warn(
        kind_now,
        legend=filled.get("legend"),
        u_variable=filled.get("u_variable"),
        v_variable=filled.get("v_variable"),
        extent=filled.get("extent"),
        cities=filled.get("cities"),
        draw_boxes=draw_boxes,
        rows=filled.get("rows"),
        columns=filled.get("columns"),
        panel_spacing=filled.get("panel_spacing"),
        subplot_title=filled.get("subplot_titles"),
        bbox=filled.get("bbox"),
        mask_geojson=filled.get("mask_geojson"),
        index=filled.get("index"),
        quiver_scale=filled.get("quiver_scale"),
        quiver_step=filled.get("quiver_step"),
        vmin=filled.get("vmin"),
        vmax=filled.get("vmax"),
        cbar_label=filled.get("cbar_label"),
        variable=filled.get("variable"),
    )
    if maybe_emit_spec(merged, dump_spec, datasets=datasets):
        return None
    if output is None:
        raise UsageError("--output is required unless --dump-spec is set")
    compiled = compile(merged, datasets, theme_registry=user_theme)
    return export(compiled, output, datasets=datasets, spec=merged)


if __name__ == "__main__":
    plot()

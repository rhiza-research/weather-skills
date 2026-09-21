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
from weather_skills_core.plot.figure import (
    DEFAULT_FONTSIZE,
    parse_figsize,
    parse_label_list,
    parse_number_list,
    parse_panel_spacing,
)
from weather_skills_core.plot.maps import parse_draw_boxes, parse_layer
from weather_skills_core.plot.spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    PATCH_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    maybe_emit_spec,
    named_datasets_from_spec,
    opened_datasets_from_spec,
    overlay_flags,
    overlay_spec,
    parse_index,
    parse_plot_patch,
    parse_plot_spec,
    resolve_flags,
    spec_from_flags,
    spec_get,
    spec_inputs_from_datasets,
    trace_at,
)
from weather_skills_core.plot.theme import deep_merge, load_user_theme

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


def _spec_data(spec):
    if spec is None:
        return {}
    if hasattr(spec, "to_dict"):
        return spec.to_dict()
    return dict(spec) if isinstance(spec, dict) else {}


def _overlay_cli_from_spec(spec_data, **cli):
    """Fill omitted CLI flags from a dumped spec. CLI values win when set."""
    out = resolve_flags(spec_data, **{k: v for k, v in cli.items() if k != "draw_box"})
    out["draw_box"] = cli.get("draw_box") or spec_get(spec_data, "draw_boxes")
    if out.get("bbox") is not None:
        out["bbox"] = tuple(out["bbox"])
    if out.get("figsize") is not None:
        out["figsize"] = tuple(out["figsize"])
    return out


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
        options = dict(item.get("options") or {})
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
            f"Warning: --legend is ignored for --kind {kind} (maps use a colorbar).",
            file=sys.stderr,
        )
    if kind == "heatmap" and (flags.get("u_variable") or flags.get("v_variable")):
        print(
            "Warning: --u-variable/--v-variable is only used with --kind windrose or "
            "--kind quiver; ignored for --kind heatmap.",
            file=sys.stderr,
        )
    if kind == "timeseries":
        for name, set_ in {
            "--extent": bool(flags.get("extent")),
            "--cities": bool(flags.get("cities")),
            "--draw-box": bool(flags.get("draw_boxes")),
            "--rows": flags.get("rows") is not None,
            "--columns": flags.get("columns") is not None,
            "--panel-spacing": flags.get("panel_spacing") is not None,
            "--subplot-title": bool(flags.get("subplot_title")),
            "--bbox": flags.get("bbox") is not None,
            "--mask-geojson": bool(flags.get("mask_geojson")),
            "--index": bool(flags.get("index")),
        }.items():
            if set_:
                print(
                    f"Warning: {name} is a map-only option; ignored for --kind {kind}.",
                    file=sys.stderr,
                )
    if kind in {"xy", "windrose"}:
        extras = {
            "--extent": bool(flags.get("extent")),
            "--cities": bool(flags.get("cities")),
            "--draw-box": bool(flags.get("draw_boxes")),
            "--rows": flags.get("rows") is not None,
            "--columns": flags.get("columns") is not None,
            "--panel-spacing": flags.get("panel_spacing") is not None,
            "--subplot-title": bool(flags.get("subplot_title")),
        }
        if kind == "xy":
            extras.update(
                {
                    "--u-variable": bool(flags.get("u_variable")),
                    "--v-variable": bool(flags.get("v_variable")),
                    "--quiver-scale": flags.get("quiver_scale") is not None,
                    "--quiver-step": flags.get("quiver_step") is not None,
                    "--vmin": flags.get("vmin") is not None,
                    "--vmax": flags.get("vmax") is not None,
                    "--cbar-label": bool(flags.get("cbar_label")),
                }
            )
            if flags.get("variable"):
                print(
                    "Warning: --variable is ignored for --kind xy; use --x-variable/--y-variable.",
                    file=sys.stderr,
                )
            if legend_used:
                print("Warning: --legend is ignored for --kind xy.", file=sys.stderr)
        if kind == "windrose":
            extras.update(
                {
                    "--quiver-scale": flags.get("quiver_scale") is not None,
                    "--quiver-step": flags.get("quiver_step") is not None,
                    "--vmin": flags.get("vmin") is not None,
                    "--vmax": flags.get("vmax") is not None,
                    "--cbar-label": bool(flags.get("cbar_label")),
                }
            )
        for name, set_ in extras.items():
            if set_:
                print(
                    f"Warning: {name} is ignored for --kind {kind}.",
                    file=sys.stderr,
                )


def _merged_spec(
    spec_data,
    *,
    kind,
    datasets,
    user_theme,
    patch,
    layers=None,
    **flags,
):
    template = flags.pop("template", None) or user_theme.get("template")
    flags["template"] = template
    shared_scale = flags.pop("shared_scale", False)
    independent_scale = flags.pop("independent_scale", False)
    layer_labels = flags.pop("layer_labels", None)
    if flags.get("colormap") is None and user_theme.get("colormap"):
        flags["colormap"] = user_theme.get("colormap")
    if flags.get("fontsize") is None and user_theme.get("fontsize") is not None:
        flags["fontsize"] = user_theme.get("fontsize")
    if spec_data:
        merged = overlay_flags(spec_data, kind=kind, **flags)
        if not merged.get("traces"):
            merged["traces"] = [{"kind": kind, "input": "a"}]
        elif kind:
            merged["traces"][0]["kind"] = kind
    else:
        merged = spec_from_flags(kind=kind, **flags)
    if patch:
        merged = deep_merge(merged, patch)
    if layers:
        named = _layer_datasets(layers)
        merged["traces"] = [{"kind": "layer"}]
        merged["layers"] = []
        inputs = []
        for i, layer in enumerate(layers):
            lid = chr(ord("a") + i)
            entry = {
                "kind": layer.kind,
                "path": str(layer.path),
                "options": dict(layer.options),
                "raw": layer.raw,
            }
            if layer.kind in _ZARR_LAYER_KINDS:
                entry["input"] = lid
                if layer.ds is not None:
                    named.setdefault(lid, layer.ds)
                item = {"id": lid, "path": str(layer.path)}
                if layer_labels and i < len(layer_labels) and layer_labels[i]:
                    item["label"] = layer_labels[i]
                inputs.append(item)
            merged["layers"].append(entry)
        if inputs:
            merged["inputs"] = inputs
        datasets = named or datasets
    elif datasets:
        merged["inputs"] = spec_inputs_from_datasets(datasets)
        if kind == "xy" and merged["inputs"]:
            if flags.get("x_variable"):
                merged["inputs"][0]["variable"] = flags["x_variable"]
            if flags.get("y_variable") and len(merged["inputs"]) > 1:
                merged["inputs"][1]["variable"] = flags["y_variable"]
    if flags.get("figsize") is not None:
        merged.setdefault("layout", {})["autosize"] = False
    if user_theme.get("max_columns") and not (flags.get("rows") or flags.get("columns")):
        merged.setdefault("layout", {}).setdefault("facet", {}).setdefault(
            "max_columns", user_theme["max_columns"]
        )
    if shared_scale:
        merged.setdefault("layout", {})["shared_colorscale"] = True
    if independent_scale:
        merged.setdefault("layout", {})["shared_colorscale"] = False
    merged["skill"] = "plot"
    return merged, datasets


@weather_skill(
    name="plot",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=False, default=None)
@weather_skill.argument(
    "--x",
    dest="x_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="X-axis Zarr for --kind xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--y",
    dest="y_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="Y-axis Zarr for --kind xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--layer",
    action="append",
    default=None,
    type=parse_layer,
    help=(
        "Map layer KIND:PATH or KIND:PATH::k=v. Repeat for overlays. "
        "Kinds: heatmap, scatter, quiver, outline, mask. "
        "Options: variable, colormap, index, u_variable, v_variable, "
        "quiver_scale, quiver_step, vmin, vmax. Mutually exclusive with -i/--input."
    ),
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--x-variable",
    default=None,
    help="X-axis variable for --kind xy. Defaults to the first data variable of --x (or -i).",
)
@weather_skill.argument(
    "--y-variable",
    default=None,
    help="Y-axis variable for --kind xy. Defaults to the first data variable of --y (or -i).",
)
@weather_skill.argument(
    "--pair-on",
    choices=["time", "year", "index"],
    default=None,
    help=(
        "How --kind xy matches --x to --y samples: shared time (default), "
        "calendar year (e.g. September IOD vs October rain), or position."
    ),
)
@weather_skill.argument(
    "--kind",
    choices=["heatmap", "contour", "timeseries", "xy", "windrose", "quiver"],
    default=None,
)
@weather_skill.argument(
    "--u-variable",
    default=None,
    help="Eastward wind variable (windrose/quiver). Auto-detected when omitted.",
)
@weather_skill.argument(
    "--v-variable",
    default=None,
    help="Northward wind variable (windrose/quiver). Auto-detected when omitted.",
)
@weather_skill.argument(
    "--colormap",
    default=None,
    help=(
        "matplotlib colormap name, or comma-separated colors. "
        "When plotting rainfall anomalies, omit this flag so the default "
        "ppt_anomaly / chirps_anom classes apply. "
        "Also: ppt_poa, ppt_spp, spi. Else rocket. "
        "Windrose default: blue-to-orange speed classes. "
        "Quiver default: YlGn (ECMWF S2S 10 m / 700 hPa wind vectors)."
    ),
)
@weather_skill.argument(
    "--colormap-bounds",
    default=None,
    type=parse_number_list,
    help=(
        "Discrete colormap class edges (comma-separated). "
        "Folds into theme.colormap.bounds. "
        "A leading minus needs --colormap-bounds=-100,100 (not a space)."
    ),
)
@weather_skill.argument("--colormap-under", default=None, help="Color below the first bound.")
@weather_skill.argument("--colormap-over", default=None, help="Color above the last bound.")
@weather_skill.argument(
    "--index",
    default=None,
    help=(
        "Slice like 'step=3,number=0' (heatmap, contour, quiver, and windrose). "
        "Heatmap/contour/quiver lists keep the dim as panels; windrose lists keep samples."
    ),
)
@weather_skill.argument(
    "--reduce",
    action="append",
    default=None,
    help=(
        "Average over this dim for --kind timeseries. Repeat once per leftover "
        "non-time dim (e.g. --reduce latitude --reduce longitude). No dim is "
        "averaged unless you say so."
    ),
)
@weather_skill.argument(
    "--along",
    default=None,
    help=(
        "Draw one --kind timeseries line per value of this dim (e.g. --along number "
        "for ensemble members) instead of reducing it."
    ),
)
@weather_skill.argument(
    "--extent",
    default=None,
    help="Map extent 'lon_min,lon_max,lat_min,lat_max' (heatmap, contour, and quiver).",
)
@weather_skill.argument(
    "--cities",
    default=None,
    help='City overlay JSON (heatmap, contour, and quiver). Inline {"name": [lat, lon]} or file path.',
)
@weather_skill.argument(
    "--fontsize",
    type=int,
    default=DEFAULT_FONTSIZE,
    help="Base font size for titles, axis labels, and colorbar text (default 16).",
)
@weather_skill.argument(
    "--figsize",
    default=None,
    type=parse_figsize,
    help="Figure size W,H inches (e.g. 10,6 or 10x6). Default is kind-specific.",
)
@weather_skill.argument(
    "--legend",
    default=None,
    type=parse_legend,
    help=(
        "Legend placement: matplotlib loc (best, upper right, …), "
        "'outside right', 'below', or 'none'. Windrose default: outside right. "
        "Timeseries draws a legend only when this is set."
    ),
)
@weather_skill.argument("--title", default=None, help="Optional figure title (above all panels).")
@weather_skill.argument(
    "--subplot-title",
    action="append",
    default=None,
    help=(
        "Override one map panel title, in panel order. Repeat for each panel. "
        "Fewer than the panel count keeps auto date/lead titles for the rest; "
        "more than the panel count is an error. Maps only."
    ),
)
@weather_skill.argument(
    "--xlabel",
    default=None,
    help="Override the x-axis label (default: Longitude; omitted on datetime ticks).",
)
@weather_skill.argument(
    "--ylabel",
    default=None,
    help="Override the y-axis label (default: Latitude / variable label / Frequency (%%)).",
)
@weather_skill.argument(
    "--cbar-label",
    default=None,
    help=(
        "Override the colorbar label (heatmap, contour, quiver, layered maps). "
        "Use the variable and units (Total precipitation [mm]), not a date. "
        "Default is the variable long_name + units. Per-layer --label wins."
    ),
)
@weather_skill.argument(
    "--cbar-ticks",
    default=None,
    type=parse_number_list,
    help="Explicit colorbar tick values (comma-separated).",
)
@weather_skill.argument(
    "--cbar-labels",
    default=None,
    type=parse_label_list,
    help="Explicit colorbar tick labels (comma-separated; requires --cbar-ticks).",
)
@weather_skill.argument(
    "--rows",
    type=int,
    default=None,
    help=(
        "Heatmap/contour/quiver panel rows. Extra cells stay blank when the grid is larger than the data."
    ),
)
@weather_skill.argument(
    "--columns",
    type=int,
    default=None,
    help=(
        "Heatmap/contour/quiver panel columns. Extra cells stay blank when the grid is larger than the data."
    ),
)
@weather_skill.argument(
    "--panel-spacing",
    default=None,
    type=parse_panel_spacing,
    help=(
        "Inter-panel gap as a fraction of panel size: W or W,H "
        "(matplotlib GridSpec wspace/hspace). Maps only. "
        "One value sets both axes. Disables compressed packing so equal-aspect "
        "map panels keep the requested whitespace."
    ),
)
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; cells/points outside become NaN (heatmap, contour, quiver, windrose, xy).",
)
@weather_skill.argument(
    "--draw-box",
    action="append",
    default=None,
    help=(
        "Draw a black outline box on the map as N/W/S/E decimal degrees "
        "(same form as --bbox). Repeat for multiple boxes. Heatmap and quiver."
    ),
)
@weather_skill.argument(
    "--quiver-scale",
    type=float,
    default=None,
    help=(
        "Matplotlib quiver scale (larger → shorter arrows). "
        "Default sizes a typical wind to ~1.5× the subsampled grid spacing. "
        "Quiver-only."
    ),
)
@weather_skill.argument(
    "--quiver-step",
    type=int,
    default=None,
    help=(
        "Plot every Nth grid point for --kind quiver "
        "(S2S plot_wind_and_sst_anomaly quiver_step). "
        "Default: 1 on ~1.5° grids; finer grids auto-thin to ~1.5°. Quiver-only."
    ),
)
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help=(
        "Colorbar label for each --layer, in order (variable/quantity, not a date). "
        "Omit to use the variable long_name + units."
    ),
)
@weather_skill.argument(
    "--shared-scale",
    action="store_true",
    help="Force one shared color scale across heatmap/scatter layers.",
)
@weather_skill.argument(
    "--independent-scale",
    action="store_true",
    help="Force a separate color scale per heatmap/scatter layer.",
)
@weather_skill.argument(
    "--vmin",
    type=float,
    default=None,
    help="Colorbar lower limit (heatmap, contour, quiver, scatter). Unset = data min.",
)
@weather_skill.argument(
    "--vmax",
    type=float,
    default=None,
    help="Colorbar upper limit (heatmap, contour, quiver, scatter). Unset = data max.",
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
    "--theme",
    default=None,
    choices=["weather_skills", "colorblind"],
    help="Seaborn colorway: weather_skills (deep) or colorblind. Default weather_skills.",
)
@weather_skill.argument(
    "--patch",
    default=None,
    type=parse_plot_patch,
    help=PATCH_ARGUMENT_HELP,
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
    bbox,
    variable,
    kind,
    colormap,
    title,
    subplot_title,
    xlabel,
    ylabel,
    cbar_label,
    index,
    reduce,
    along,
    extent,
    cities,
    fontsize,
    figsize,
    legend,
    mask_geojson,
    draw_box,
    rows,
    columns,
    panel_spacing,
    u_variable,
    v_variable,
    quiver_scale,
    quiver_step,
    output,
    layer=None,
    label=None,
    shared_scale=False,
    independent_scale=False,
    x_ds=None,
    y_ds=None,
    x_variable=None,
    y_variable=None,
    pair_on=None,
    vmin=None,
    vmax=None,
    spec=None,
    theme_file=None,
    theme=None,
    patch=None,
    dump_spec=None,
    colormap_bounds=None,
    colormap_under=None,
    colormap_over=None,
    cbar_ticks=None,
    cbar_labels=None,
    **kwargs,
):
    """Render a heatmap, contour, timeseries, xy scatter, wind-rose, quiver, or layered map PNG from weather-skills Zarrs."""
    spec_data = _spec_data(spec)
    if patch:
        spec_data = overlay_spec(spec_data, patch)
    filled = _overlay_cli_from_spec(
        spec_data,
        title=title,
        xlabel=xlabel,
        ylabel=ylabel,
        cbar_label=cbar_label,
        colormap=colormap,
        legend=legend,
        index=index,
        u_variable=u_variable,
        v_variable=v_variable,
        variable=variable,
        bbox=bbox,
        mask_geojson=mask_geojson,
        extent=extent,
        cities=cities,
        draw_box=draw_box,
        figsize=figsize,
        vmin=vmin,
        vmax=vmax,
        reduce=reduce,
        along=along,
        rows=rows,
        columns=columns,
        panel_spacing=panel_spacing,
        pair_on=pair_on,
        x_variable=x_variable,
        y_variable=y_variable,
        quiver_scale=quiver_scale,
        quiver_step=quiver_step,
        colormap_bounds=colormap_bounds,
        colormap_under=colormap_under,
        colormap_over=colormap_over,
        cbar_ticks=cbar_ticks,
        cbar_labels=cbar_labels,
        kind=kind,
    )
    title = filled["title"]
    xlabel = filled["xlabel"]
    ylabel = filled["ylabel"]
    cbar_label = filled["cbar_label"]
    colormap = filled["colormap"]
    legend = filled["legend"]
    index = filled["index"]
    u_variable = filled["u_variable"]
    v_variable = filled["v_variable"]
    variable = filled["variable"]
    bbox = filled["bbox"]
    mask_geojson = filled["mask_geojson"]
    extent = filled["extent"]
    cities = filled["cities"]
    draw_box = filled["draw_box"]
    figsize = filled["figsize"]
    vmin = filled["vmin"]
    vmax = filled["vmax"]
    kind = filled.get("kind") or kind
    layers = list(layer or [])
    if not layers:
        layers = _layers_from_spec(spec_data, spec)
    if kind is None and not layers:
        kind = _kind_from_spec(spec_data) or "heatmap"
    if spec is not None and ds is None and not layers and kind != "xy":
        ds = spec.ds if spec.ds is not None else None
        if ds is None and spec.datasets:
            ds = spec.datasets[0]
    if layers and ds is not None:
        raise UsageError("pass either -i/--input or --layer, not both")
    if layers and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either --layer or --x/--y, not both")
    if ds is not None and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either -i/--input or --x/--y, not both")
    if kind == "xy":
        x_ds, y_ds, x_variable, y_variable, pair_on = _xy_from_spec(
            spec_data, spec, x_ds, y_ds, x_variable, y_variable, pair_on
        )
        if layers:
            raise UsageError("--layer cannot be used with --kind xy")
        if x_ds is None and y_ds is None:
            if ds is None:
                raise UsageError(
                    "--kind xy needs --x and --y, or -i with --x-variable and --y-variable"
                )
            if not x_variable or not y_variable:
                raise UsageError(
                    "with a single -i, --kind xy needs both --x-variable and --y-variable"
                )
            x_ds = ds
            y_ds = ds
        elif x_ds is None or y_ds is None:
            raise UsageError("--kind xy needs both --x and --y")
    elif not layers and ds is None:
        raise UsageError("pass -i/--input, a --spec with inputs, or at least one --layer")
    if layers and kind in ("timeseries", "xy", "windrose", "contour"):
        raise UsageError(f"--layer cannot be used with --kind {kind}")
    if layers and kind == "quiver":
        raise UsageError(
            "with --layer, draw wind vectors as --layer quiver:PATH instead of --kind quiver"
        )
    if layers and legend is not None and legend != "none":
        print(
            "Warning: --legend is ignored for layered maps (they use colorbars).",
            file=sys.stderr,
        )

    parse_index(index)
    draw_boxes = parse_draw_boxes(draw_box)
    _warn(
        "layer" if layers else kind,
        legend=legend,
        u_variable=u_variable,
        v_variable=v_variable,
        extent=extent,
        cities=cities,
        draw_boxes=draw_boxes,
        rows=rows,
        columns=columns,
        panel_spacing=panel_spacing,
        subplot_title=subplot_title,
        bbox=bbox,
        mask_geojson=mask_geojson,
        index=index,
        quiver_scale=quiver_scale,
        quiver_step=quiver_step,
        vmin=vmin,
        vmax=vmax,
        cbar_label=cbar_label,
        variable=variable,
    )

    user_theme = load_user_theme(theme_file)
    if kind == "xy":
        datasets = {"a": x_ds} if x_ds is y_ds else {"x": x_ds, "y": y_ds}
    elif layers:
        datasets = {}
    else:
        datasets = {"a": ds}
        if spec is not None and spec.datasets:
            for i, extra in enumerate(spec.datasets):
                key = (
                    (spec.data.get("inputs") or [{}])[i].get("id")
                    if spec.data.get("inputs")
                    else None
                )
                datasets[key or f"i{i}"] = extra
            datasets["a"] = ds

    merged, datasets = _merged_spec(
        spec_data,
        kind=kind if not layers else "layer",
        datasets=datasets,
        user_theme=user_theme,
        patch=None,
        layers=layers or None,
        input_path=_input_path_of(ds) or _input_path_of(x_ds),
        variable=variable,
        index=index,
        reduce=reduce,
        along=along,
        title=title,
        subplot_titles=list(subplot_title) if subplot_title else None,
        xlabel=xlabel,
        ylabel=ylabel,
        cbar_label=cbar_label,
        legend=legend,
        vmin=vmin,
        vmax=vmax,
        colormap=colormap,
        colormap_bounds=colormap_bounds or filled.get("colormap_bounds"),
        colormap_under=colormap_under or filled.get("colormap_under"),
        colormap_over=colormap_over or filled.get("colormap_over"),
        cbar_ticks=cbar_ticks or filled.get("cbar_ticks"),
        cbar_labels=cbar_labels or filled.get("cbar_labels"),
        fontsize=fontsize,
        template=theme,
        figsize=figsize,
        rows=rows,
        columns=columns,
        panel_spacing=panel_spacing,
        extent=extent,
        cities=cities,
        bbox=bbox,
        mask_geojson=mask_geojson,
        draw_boxes=draw_boxes,
        pair_on=pair_on if kind == "xy" else None,
        x_variable=x_variable if kind == "xy" else None,
        y_variable=y_variable if kind == "xy" else None,
        u_variable=u_variable,
        v_variable=v_variable,
        quiver_scale=quiver_scale,
        quiver_step=quiver_step,
        shared_scale=shared_scale,
        independent_scale=independent_scale,
        layer_labels=label,
    )
    if maybe_emit_spec(merged, dump_spec, datasets=datasets):
        return None
    if output is None:
        raise UsageError("--output is required unless --dump-spec is set")
    compiled = compile(merged, datasets, theme_registry=user_theme)
    return export(
        compiled,
        output,
        datasets=datasets,
        spec=merged,
    )


if __name__ == "__main__":
    plot()

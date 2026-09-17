# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@plot-refactor",
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
import json
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.display_labels import dataset_display_label, resolve_input_labels
from weather_skills_core.figure import (
    DEFAULT_FONTSIZE,
    add_shared_colorbar,
    apply_date_ticks,
    apply_style,
    axis_label,
    format_plot_date,
    is_datetime_axis,
    parse_figsize,
    resolve_axis_label,
    resolve_figsize,
    resolve_time_axis_label,
)
from weather_skills_core.plot_compile import (
    axis_kind,
    figsize_from_extent,
    format_step,
    pad_cell_extent,
    panel_title,
    parse_cities,
    parse_draw_boxes,
    parse_extent,
    plain,
    step_dim,
    subset_spatial,
    timeseries_axis,
)
from weather_skills_core.plot_mpl import (
    apply_rc,
    finish_figure,
    mesh_kwargs,
    quiver_kwargs,
    windrose_kwargs,
)
from weather_skills_core.plot_spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    PlotSpec,
    apply_index,
    dump_spec_dest,
    named_datasets_from_spec,
    opened_datasets_from_spec,
    overlay_spec,
    panel_shape,
    parse_index,
    parse_plot_spec,
    spec_from_flags,
    spec_inputs_from_datasets,
)
from weather_skills_core.plot_style import (
    DISCRETE_PRECIP_NAMES,
    PRECIP_ANOMALY_BOUNDS,
    PRECIP_BOUNDS,
    PRECIP_SHORT_BOUNDS,
    load_user_style,
)
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    polygon_from_geojson,
)
from weather_skills_core.units import (
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_label_for_display,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# Tests assert against these palettes via `plot_mod.PRECIP_*`.
_PRECIP_PALETTE_BOUNDS = (PRECIP_BOUNDS, PRECIP_SHORT_BOUNDS, PRECIP_ANOMALY_BOUNDS)

_apply_date_ticks = apply_date_ticks
_apply_index = apply_index
_axis_kind = axis_kind
_axis_label = axis_label
_figsize_from_extent = figsize_from_extent
_format_date = format_plot_date
_format_step = format_step
_is_datetime_axis = is_datetime_axis
_pad_cell_extent = pad_cell_extent
_panel_shape = panel_shape
_panel_title = panel_title
_parse_cities = parse_cities
_parse_draw_boxes = parse_draw_boxes
_parse_extent = parse_extent
_parse_index = parse_index
_plain = plain
_resolve_axis_label = resolve_axis_label
_resolve_time_axis_label = resolve_time_axis_label
_step_dim = step_dim
_subset_spatial = subset_spatial
_timeseries_axis = timeseries_axis

# Meteorological wind rose: 16 compass sectors, speed stacked in m/s classes.
WIND_ROSE_SECTORS = 16
WIND_SPEED_EDGES_MS = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
WIND_SPEED_COLORS = [
    "#c6dbef",
    "#6baed6",
    "#2171b5",
    "#08306b",
    "#fd8d3c",
    "#d94801",
    "#7f2704",
]
_UV_NAME_PAIRS = (
    ("u10", "v10"),
    ("u100", "v100"),
    ("10u", "10v"),
    ("uas", "vas"),
    ("ua", "va"),
    ("u", "v"),
    ("eastward_wind", "northward_wind"),
    ("10m_u_component_of_wind", "10m_v_component_of_wind"),
    ("100m_u_component_of_wind", "100m_v_component_of_wind"),
    ("u_component_of_wind", "v_component_of_wind"),
    ("uwind", "vwind"),
    ("uwnd", "vwnd"),
)
_SAMPLE_DIM_NAMES = {"step", "number", "point_id", "station_id", "valid_time"}

# Speed field matches plot_s2s 10 m / 700 hPa (YlGn). Arrows sit on the
# native grid (plot_wind_and_sst_anomaly), thinned to ~1.5°. Scale is
# auto-picked so a typical wind is ~1.5× that spacing — a fixed 100 matches
# S2S *anomaly* magnitudes and overdraws 10 m/s basin winds.
QUIVER_CMAP = "YlGn"
QUIVER_SCALE = 100.0
QUIVER_STEP = 1
QUIVER_TARGET_SPACING_DEG = 1.5
QUIVER_ARROW_LEN_SPACING = 1.5
QUIVER_KEY_MS = (5.0, 10.0)

# Natural Earth scale vs map span (max of lon/lat extent in degrees).
# Admin-1 (states / provinces / counties) is only readable on country-scale
# views; a multi-country or basin map would be a thicket of province lines.
_ADMIN1_MAX_SPAN_DEG = 20.0
_HIRES_MAX_SPAN_DEG = 45.0
_MIDRES_MAX_SPAN_DEG = 90.0

# KMD-style water fill (Lake Victoria, Turkana, …) drawn on top of the heatmap.
_LAKE_FACECOLOR = "#4da6ff"
_ADMIN1_STYLE = {"facecolor": "none", "edgecolor": "0.45", "linewidth": 0.4, "zorder": 3}
_LAKES_STYLE = {
    "facecolor": _LAKE_FACECOLOR,
    "edgecolor": _LAKE_FACECOLOR,
    "linewidth": 0.4,
    "zorder": 3.5,
}
_BORDERS_STYLE = {"facecolor": "none", "edgecolor": "0.15", "linewidth": 0.8, "zorder": 4}
_COAST_STYLE = {"facecolor": "none", "edgecolor": "black", "linewidth": 0.8, "zorder": 4}

_LAYER_KINDS = frozenset({"heatmap", "scatter", "quiver", "outline", "mask"})
_ZARR_LAYER_KINDS = frozenset({"heatmap", "scatter", "quiver"})
_LAYER_OPTION_KEYS = frozenset(
    {
        "variable",
        "colormap",
        "index",
        "u-variable",
        "v-variable",
        "quiver-scale",
        "quiver-step",
        "vmin",
        "vmax",
    }
)
_KIND_ZORDER = {"heatmap": 1.0, "quiver": 5.0, "scatter": 6.0, "outline": 7.0}


class LayerSpec:
    """One ``--layer KIND:PATH[::k=v]`` entry. The decorator may set ``.ds``."""

    def __init__(self, kind, path, options, raw):
        self.kind = kind
        self.path = Path(path)
        self.options = options
        self.raw = raw
        self.ds = None

    def zarr_paths(self):
        if self.kind in _ZARR_LAYER_KINDS:
            return [self.path]
        return []

    def __str__(self):
        return self.raw

    def __repr__(self):
        return f"LayerSpec({self.raw!r})"


def _parse_layer_options(blob):
    """Parse ``k=v,k=v``; tokens without ``=`` continue the previous value (for ``index=step=0,1,2``)."""
    options = {}
    current = None
    for token in blob.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            key, _, val = token.partition("=")
            key = key.strip()
            if not key:
                raise ValueError(f"--layer option {token!r} has an empty key")
            if key not in _LAYER_OPTION_KEYS:
                raise ValueError(
                    f"unknown --layer option {key!r}; "
                    f"expected one of {', '.join(sorted(_LAYER_OPTION_KEYS))}"
                )
            if key in options:
                raise ValueError(f"--layer option {key!r} is given more than once")
            current = key
            options[key] = val.strip()
        else:
            if current is None:
                raise ValueError(f"--layer option {token!r} appears before any key=value")
            options[current] = f"{options[current]},{token}"
    return options


def parse_layer(value):
    """Argparse converter for ``KIND:PATH`` or ``KIND:PATH::k=v[,k=v...]``."""
    if not value or not str(value).strip():
        raise argparse.ArgumentTypeError("--layer spec is empty")
    raw = str(value).strip()
    if "::" in raw:
        head, _, opt_blob = raw.partition("::")
    else:
        head, opt_blob = raw, ""
    if ":" not in head:
        raise argparse.ArgumentTypeError(
            f"--layer {raw!r} must be KIND:PATH (e.g. heatmap:/tmp/a.zarr)"
        )
    kind, _, path = head.partition(":")
    kind = kind.strip().lower()
    path = path.strip()
    if kind not in _LAYER_KINDS:
        raise argparse.ArgumentTypeError(
            f"unknown --layer kind {kind!r}; expected one of {', '.join(sorted(_LAYER_KINDS))}"
        )
    if not path:
        raise argparse.ArgumentTypeError(f"--layer {raw!r} is missing a path")
    try:
        options = _parse_layer_options(opt_blob) if opt_blob else {}
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return LayerSpec(kind, path, options, raw)


def parse_json_object(value):
    """Argparse converter for a JSON object (inline or file path)."""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    path = Path(raw)
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"expected a JSON object: {exc}") from None
    if not isinstance(data, dict):
        raise argparse.ArgumentTypeError("JSON patch must be an object")
    return data


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


_OUTSIDE_LEGEND_LOCS = {
    "outside right": "outside right",
    "below": "outside lower center",
}


def _legend_kwargs(loc, *, default="outside right"):
    """Matplotlib ``legend()`` kwargs, or ``None`` to omit the legend."""
    resolved = default if loc is None else loc
    if resolved == "none":
        return None
    if resolved in _OUTSIDE_LEGEND_LOCS:
        return {"loc": _OUTSIDE_LEGEND_LOCS[resolved]}
    return {"loc": resolved}


def _place_legend(ax, loc, *, default="none", **extra):
    """Draw an axes or figure legend. Outside placements stay on-canvas."""
    kw = _legend_kwargs(loc, default=default)
    if kw is None:
        return None
    extra = {"frameon": False, **extra}
    loc_name = kw.get("loc")
    if isinstance(loc_name, str) and loc_name.startswith("outside"):
        return ax.figure.legend(**kw, **extra)
    return ax.legend(**kw, **extra)


def _rotate_date_labels(ax) -> None:
    """Rotate date tick labels without ``subplots_adjust`` (constrained-layout safe)."""
    for label in ax.get_xticklabels():
        label.set_ha("right")
        label.set_rotation(30)


def _subset_points(da, bbox_nwse, region_polygon):
    """Filter station / point samples to ``--bbox`` / ``--mask-geojson``."""
    import numpy as np
    import xarray as xr

    lat_name = cf_dim(da, "latitude")
    lon_name = cf_dim(da, "longitude")
    if lat_name is None or lon_name is None:
        raise UsageError(
            f"--bbox/--mask-geojson need latitude/longitude coordinates; got dims {list(da.dims)}"
        )
    lat = np.asarray(da[lat_name].values)
    lon = np.asarray(da[lon_name].values)
    keep = np.ones(np.broadcast(lat, lon).shape, dtype=bool)
    lat_b, lon_b = np.broadcast_arrays(lat, lon)
    if bbox_nwse is not None:
        r_n, r_w, r_s, r_e = bbox_nwse
        keep &= (lat_b >= r_s) & (lat_b <= r_n)
        if r_w > r_e:
            keep &= (lon_b >= r_w) | (lon_b <= r_e)
        else:
            keep &= (lon_b >= r_w) & (lon_b <= r_e)
    if region_polygon is not None:
        import shapely

        keep &= shapely.contains_xy(region_polygon, lon_b, lat_b)
        if not bool(keep.any()):
            print(
                "Warning: --mask-geojson polygon does not intersect the points; "
                "the rose will be empty.",
                file=sys.stderr,
            )
    keep_da = xr.DataArray(keep, dims=da[lat_name].dims)
    return da.where(keep_da, drop=True)


def _prepare_gridded_map(
    da, overrides, bbox_nwse, mask_geojson, extent, *, style, region_polygon=None
):
    """Index, bbox, and mask a lat/lon field for a map panel. Returns a tuple.

    ``(da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps)``.
    """
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if lat_dim is None or lon_dim is None:
        raise UsageError(f"{style} requires lat/lon coords; got {list(da.dims)}.")
    if lat_dim not in da.dims or lon_dim not in da.dims:
        raise UsageError(
            f"{style} needs lat/lon as dimensions, but {lat_dim!r}/"
            f"{lon_dim!r} are non-dimension coordinates here (dims: "
            f"{list(da.dims)}); station data has no 2D grid to plot."
        )
    native_step_dim = _step_dim(da)
    native_steps = list(da[native_step_dim].values) if native_step_dim else None
    list_dims = (native_step_dim,) if native_step_dim else ()
    da = _apply_index(da, overrides, list_dims=list_dims)
    for spatial_dim in (lat_dim, lon_dim):
        if spatial_dim in overrides and spatial_dim not in da.dims:
            raise UsageError(
                f"--index removed the {spatial_dim!r} dimension; {style} needs a 2D lat/lon grid"
            )
    panel_dim = _step_dim(da)
    for dim in da.dims:
        if dim not in (panel_dim, "number", lat_dim, lon_dim):
            panel_desc = repr(panel_dim) if panel_dim else "step/time"
            raise UsageError(
                f"dimension {dim!r} remains after selection; {style} "
                f"panels only the {panel_desc} dimension — select a position "
                f"from {dim!r} with --index"
            )
    if panel_dim is not None and da.sizes[panel_dim] == 0:
        raise UsageError(f"dimension {panel_dim!r} has size 0; nothing to plot.")
    extent_vals = _parse_extent(extent)
    if region_polygon is None and mask_geojson:
        region_polygon = polygon_from_geojson(mask_geojson)
    wrapped_bbox = bbox_nwse is not None and bbox_nwse[1] > bbox_nwse[3]
    da, extent_vals = _subset_spatial(da, lat_dim, lon_dim, bbox_nwse, region_polygon, extent_vals)
    if da.sizes[lat_dim] == 0 or da.sizes[lon_dim] == 0:
        raise UsageError(
            "selection produced an empty grid (no cells remain after "
            "--index/--bbox selection); nothing to plot."
        )
    return da, lat_dim, lon_dim, extent_vals, not wrapped_bbox, native_step_dim, native_steps


def _parse_colormap(spec):
    if spec is None or "," not in spec:
        return spec
    from matplotlib.colors import LinearSegmentedColormap

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    return LinearSegmentedColormap.from_list("custom", parts)


def _flag_values(da):
    """Sorted CF ``flag_values``, or None."""
    import numpy as np

    raw = da.attrs.get("flag_values")
    if raw is None:
        return None
    values = np.asarray(raw, dtype=float).ravel()
    if values.size < 2:
        return None
    return np.sort(values)


def _discrete_flag_scale(da, colormap):
    """ListedColormap + BoundaryNorm for CF flag fields, or None."""
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap

    values = _flag_values(da)
    if values is None:
        return None
    meanings = da.attrs.get("flag_meanings")
    labels = None
    if isinstance(meanings, str) and meanings.strip():
        parts = meanings.split()
        raw = np.asarray(da.attrs.get("flag_values"), dtype=float).ravel()
        if parts and len(parts) == raw.size:
            labels = [parts[i] for i in np.argsort(raw)]
    colors = None
    if colormap and "," in colormap:
        parts = [p.strip() for p in colormap.split(",") if p.strip()]
        if len(parts) == values.size:
            colors = parts
    if colors is None:
        if values.size == 3:
            colors = ["#d73027", "#f0f0f0", "#1a9850"]
        else:
            from matplotlib import colormaps

            tab = colormaps["tab10"](np.linspace(0, 1, values.size))
            colors = [tuple(c) for c in tab]
    mids = (values[:-1] + values[1:]) / 2.0
    bounds = np.concatenate(([values[0] - 0.5], mids, [values[-1] + 0.5]))
    cmap = ListedColormap(colors)
    return cmap, BoundaryNorm(bounds, cmap.N), values, labels


def _heatmap_scale(da, colormap, *, stretch=False):
    """Return ``(cmap, norm)``. ``norm`` is set for the default precip scale.

    ``stretch=True`` (user ``--vmin`` / ``--vmax``) keeps the CHC colors
    but drops ``BoundaryNorm`` so the colorbar can use arbitrary limits.
    """
    from weather_skills_core.plot_style import mpl_cmap_norm, resolve_colorscale

    if colormap:
        return _parse_colormap(colormap), None
    scale = resolve_colorscale(da, None, stretch=stretch)
    if stretch:
        colors = scale.get("colors")
        if colors:
            return _parse_colormap(",".join(colors)), None
        return scale.get("cmap") or scale.get("name"), None
    if scale.get("bounds"):
        return mpl_cmap_norm(scale)
    return scale.get("cmap") or scale.get("name") or "rocket", None


def _layer_optional_float(spec, key):
    raw = spec.options.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise UsageError(f"--layer option {key}={raw!r} is not a number") from exc


def _resolve_color_limits(da, vmin=None, vmax=None, *, norm=None, flag="--vmin/--vmax"):
    """Resolve colorbar limits. User limits drop a discrete ``BoundaryNorm``.

    When both limits are omitted, diverging data is recentered on zero.
    """
    import numpy as np
    from matplotlib.colors import BoundaryNorm

    user_set = vmin is not None or vmax is not None
    if user_set and isinstance(norm, BoundaryNorm):
        norm = None
    if norm is not None and not user_set:
        return None, None, norm

    data_min = float(da.min(skipna=True).values)
    data_max = float(da.max(skipna=True).values)
    if not np.isfinite(data_min) or not np.isfinite(data_max):
        data_min, data_max = 0.0, 1.0
    lo = data_min if vmin is None else float(vmin)
    hi = data_max if vmax is None else float(vmax)
    if not user_set and hi > 0 and lo < 0:
        m = max(abs(hi), abs(lo))
        lo, hi = -m, m
    if lo > hi:
        raise UsageError(f"{flag}: lower limit {lo} is greater than upper limit {hi}")
    if lo == hi:
        pad = abs(lo) * 0.05 if lo != 0 else 1.0
        lo, hi = lo - pad, hi + pad
    return lo, hi, None


def _cbar_extend_for_limits(da, vmin, vmax):
    """``colorbar(extend=...)`` when data sits outside the user limits."""
    import numpy as np

    if vmin is None or vmax is None:
        return None
    data_min = float(da.min(skipna=True).values)
    data_max = float(da.max(skipna=True).values)
    lo = np.isfinite(data_min) and data_min < vmin
    hi = np.isfinite(data_max) and data_max > vmax
    if lo and hi:
        return "both"
    if lo:
        return "min"
    if hi:
        return "max"
    return None


def _cbar_boundary_kwargs(norm, cmap=None):
    """Colorbar kwargs for a BoundaryNorm scale (ticks, spacing, optional extend)."""
    from matplotlib.colors import BoundaryNorm

    if not isinstance(norm, BoundaryNorm):
        return {}
    kw = {"spacing": "uniform", "ticks": list(norm.boundaries)}
    if getattr(cmap, "name", None) in DISCRETE_PRECIP_NAMES:
        kw["extend"] = "both"
    return kw


def _variable_label(da):
    """Colorbar / axis label from CF ``long_name`` (then GRIB_name, then the name)."""
    return variable_label_for_display(da)


def _draw_boxes_on_ax(ax, boxes, transform):
    """Outline each N/W/S/E box in black (split antimeridian spans into two)."""
    from matplotlib.patches import Rectangle

    for north, west, south, east in boxes:
        height = north - south
        if west <= east:
            ax.add_patch(
                Rectangle(
                    (west, south),
                    east - west,
                    height,
                    fill=False,
                    edgecolor="black",
                    linewidth=1.5,
                    transform=transform,
                    zorder=5,
                )
            )
        else:
            # Antimeridian: west..180 and -180..east
            ax.add_patch(
                Rectangle(
                    (west, south),
                    180.0 - west,
                    height,
                    fill=False,
                    edgecolor="black",
                    linewidth=1.5,
                    transform=transform,
                    zorder=5,
                )
            )
            ax.add_patch(
                Rectangle(
                    (-180.0, south),
                    east - (-180.0),
                    height,
                    fill=False,
                    edgecolor="black",
                    linewidth=1.5,
                    transform=transform,
                    zorder=5,
                )
            )


def _extent_span_deg(extent):
    lon_min, lon_max, lat_min, lat_max = extent
    return max(abs(lon_max - lon_min), abs(lat_max - lat_min))


def _boundary_layers(extent):
    """Natural Earth scale and whether to overlay admin-1 for this view."""
    span = _extent_span_deg(extent)
    if span > _MIDRES_MAX_SPAN_DEG:
        return {"scale": "110m", "admin1": False}
    if span > _HIRES_MAX_SPAN_DEG:
        return {"scale": "50m", "admin1": False}
    return {"scale": "10m", "admin1": span <= _ADMIN1_MAX_SPAN_DEG}


def _extent_clip_geom(extent):
    """Shapely clip geometry for ``lon_min,lon_max,lat_min,lat_max``.

    Antimeridian views store a continuous unwrapped lon (e.g. 170..190) which
    is split back into ``[-180, 180]`` pieces for Natural Earth intersection.
    """
    from shapely.geometry import box

    lon_min, lon_max, lat_min, lat_max = extent
    if lon_max > 180.0:
        return box(lon_min, lat_min, 180.0, lat_max).union(
            box(-180.0, lat_min, lon_max - 360.0, lat_max)
        )
    if lon_min > lon_max:
        return box(lon_min, lat_min, 180.0, lat_max).union(box(-180.0, lat_min, lon_max, lat_max))
    return box(lon_min, lat_min, lon_max, lat_max)


def _unwrap_geoms(geoms, lon_min):
    """Shift western-hemisphere pieces so they match an unwrapped lon axis."""
    import numpy as np
    import shapely

    def shift(coords):
        out = np.asarray(coords).copy()
        out[:, 0] = np.where(out[:, 0] < lon_min, out[:, 0] + 360.0, out[:, 0])
        return out

    shifted = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        shifted.append(shapely.transform(geom, shift))
    return shifted


def _clip_ne_geoms(resolution, category, name, clip_geom):
    """Natural Earth geometries intersecting ``clip_geom`` (eager download)."""
    import cartopy.io.shapereader as shpreader

    path = shpreader.natural_earth(resolution=resolution, category=category, name=name)
    geoms = []
    for geom in shpreader.Reader(path).geometries():
        if geom is None or geom.is_empty:
            continue
        try:
            if not geom.intersects(clip_geom):
                continue
            clipped = geom.intersection(clip_geom)
        except Exception:  # noqa: BLE001
            clipped = geom
        if clipped is None or clipped.is_empty:
            continue
        if clipped.geom_type == "GeometryCollection":
            geoms.extend(g for g in clipped.geoms if g is not None and not g.is_empty)
        else:
            geoms.append(clipped)
    return geoms


def _load_geo_overlays(extent):
    """Scale-appropriate coastline / border / filled-lake / admin-1 overlays.

    Each layer is clipped to the map extent so a country-scale view does not
    draw the rest of the world. Download or clip failures warn and skip that
    layer — the heatmap still renders.
    """
    spec = _boundary_layers(extent)
    try:
        clip = _extent_clip_geom(extent)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: geographic overlays unavailable ({exc}); skipping.", file=sys.stderr)
        return []
    lon_min, lon_max = extent[0], extent[1]
    layers = []

    def add(category, name, style, resolution=None):
        res = resolution or spec["scale"]
        try:
            geoms = _clip_ne_geoms(res, category, name, clip)
        except Exception as exc:  # noqa: BLE001
            print(
                f"Warning: {name} overlay unavailable ({exc}); skipping.",
                file=sys.stderr,
            )
            return
        if lon_max > 180.0:
            geoms = _unwrap_geoms(geoms, lon_min)
        if geoms:
            layers.append((geoms, style))

    if spec["admin1"]:
        add("cultural", "admin_1_states_provinces", _ADMIN1_STYLE, resolution="10m")
    add("physical", "lakes", _LAKES_STYLE)
    add("cultural", "admin_0_boundary_lines_land", _BORDERS_STYLE)
    add("physical", "coastline", _COAST_STYLE)
    return layers


def _draw_geo_overlays(ax, overlays, crs):
    for geoms, style in overlays:
        ax.add_geometries(geoms, crs, **style)


def _calendar_year(value) -> int:
    """Calendar year from a datetime-like sample (numpy, cftime, or datetime)."""
    import numpy as np

    if hasattr(value, "year"):
        return int(value.year)
    arr = np.asarray(value)
    if arr.dtype.kind == "M":
        return int(arr.astype("datetime64[Y]").astype(int) + 1970)
    raise UsageError(f"--pair-on year needs datetime samples; got {value!r}")


def _pair_key(value, pair_on: str):
    """Hashable alignment key for one sample along ``--pair-on``."""
    import numpy as np

    if pair_on == "year":
        return _calendar_year(value)
    arr = np.asarray(value)
    if arr.dtype.kind == "M":
        return str(np.datetime64(arr, "D"))
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return value


def _xy_1d(ds, variable, overrides, bbox_nwse, region_polygon, role: str):
    """Reduce one input to a 1D series plus pairing-axis values."""
    import numpy as np

    variable = variable or auto_variable(ds)
    if not variable or variable not in ds:
        raise UsageError(
            f"{role} has no usable variable {variable!r}. Available: {list(ds.data_vars)}"
        )
    try:
        ds = to_standard_units(ds, variables=[variable])
    except UsageError:
        # Totals (mm) or anomalies can still carry a rate/temp standard_name
        # that classify_variable would force into an incompatible target.
        pass
    ds = precip_for_display(ds, variable)
    da = _apply_index(_plain(ds[variable]), overrides, list_dims=())
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if (bbox_nwse is not None or region_polygon is not None) and lat_dim and lon_dim:
        if lat_dim in da.dims and lon_dim in da.dims:
            da, _ = _subset_spatial(da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
    sdim = "step" if "step" in da.dims else cf_dim(da, "time")
    if sdim is None:
        if da.ndim == 1:
            sdim = da.dims[0]
        else:
            raise UsageError(f"{role} needs a time/step axis to pair samples; got {list(da.dims)}.")
    reduce_dims = [d for d in da.dims if d != sdim]
    reduced = da.mean(reduce_dims, keep_attrs=True) if reduce_dims else da
    axis_vals, _ = _timeseries_axis(reduced, sdim)
    values = np.asarray(_plain(reduced).values, dtype=float)
    return reduced, np.asarray(axis_vals), values


def _pair_xy(x_axis, x_vals, y_axis, y_vals, pair_on: str):
    """Inner-join two 1D series on time, calendar year, or position."""
    import numpy as np

    x_vals = np.asarray(x_vals, dtype=float)
    y_vals = np.asarray(y_vals, dtype=float)
    if pair_on == "index":
        if x_vals.size != y_vals.size:
            raise UsageError(
                f"--pair-on index needs the same number of samples "
                f"(--x has {x_vals.size}, --y has {y_vals.size})."
            )
        keys = list(range(x_vals.size))
        return x_vals, y_vals, keys

    x_keys = [_pair_key(v, pair_on) for v in np.ravel(x_axis)]
    y_keys = [_pair_key(v, pair_on) for v in np.ravel(y_axis)]
    x_map: dict = {}
    for i, key in enumerate(x_keys):
        if key in x_map:
            raise UsageError(
                f"--pair-on {pair_on} has duplicate {key!r} on --x; "
                "aggregate or select so each key appears once."
            )
        x_map[key] = i
    y_map: dict = {}
    for i, key in enumerate(y_keys):
        if key in y_map:
            raise UsageError(
                f"--pair-on {pair_on} has duplicate {key!r} on --y; "
                "aggregate or select so each key appears once."
            )
        y_map[key] = i
    shared = [key for key in x_keys if key in y_map]
    if not shared:
        raise UsageError(f"--pair-on {pair_on} found no matching samples between --x and --y.")
    x_out = np.array([x_vals[x_map[k]] for k in shared], dtype=float)
    y_out = np.array([y_vals[y_map[k]] for k in shared], dtype=float)
    return x_out, y_out, shared


def _plot_xy(
    x_ds,
    y_ds,
    x_variable,
    y_variable,
    pair_on,
    overrides,
    bbox_nwse,
    mask_geojson,
    title,
    xlabel,
    ylabel,
    fontsize,
    figsize=None,
):
    """Scatter --x against --y after reducing each input to 1D and pairing samples."""
    import matplotlib.pyplot as plt
    import numpy as np

    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    x_da, x_axis, x_raw = _xy_1d(x_ds, x_variable, overrides, bbox_nwse, region_polygon, "--x")
    y_da, y_axis, y_raw = _xy_1d(y_ds, y_variable, overrides, bbox_nwse, region_polygon, "--y")
    x_vals, y_vals, keys = _pair_xy(x_axis, x_raw, y_axis, y_raw, pair_on)
    finite = np.isfinite(x_vals) & np.isfinite(y_vals)
    x_vals, y_vals = x_vals[finite], y_vals[finite]
    keys = [k for k, keep in zip(keys, finite, strict=True) if keep]
    if x_vals.size == 0:
        raise UsageError("xy scatter has no finite paired samples to plot.")

    fig, ax = plt.subplots(
        figsize=resolve_figsize(figsize, (8, 6)),
        layout="constrained",
    )
    ax.scatter(x_vals, y_vals, s=36, zorder=3)
    if pair_on == "year" or (pair_on == "time" and x_vals.size <= 25):
        for xv, yv, key in zip(x_vals, y_vals, keys, strict=True):
            ax.annotate(
                str(key),
                (xv, yv),
                textcoords="offset points",
                xytext=(4, 4),
                fontsize=max(8, int(round(fontsize * 0.55))),
            )
    ax.set_xlabel(_resolve_axis_label(xlabel, _variable_label(x_da)))
    ax.set_ylabel(_resolve_axis_label(ylabel, _variable_label(y_da)))
    x_qty = variable_label_for_display(x_da, include_units=False)
    y_qty = variable_label_for_display(y_da, include_units=False)
    ax.set_title(title or f"{y_qty} vs {x_qty}")
    ax.grid(True, alpha=0.3)
    return fig


def _resolve_subplot_titles(overrides, n_panels):
    """Return user panel titles; extra flags are an error, fewer fall back to auto."""
    titles = list(overrides or [])
    if len(titles) > n_panels:
        raise UsageError(
            f"--subplot-title was passed {len(titles)} time(s) but this figure "
            f"has {n_panels} panel(s)"
        )
    return titles


def _set_panel_title(ax, index, auto, subplot_titles):
    """Apply ``--subplot-title`` when given for this panel; otherwise ``auto``."""
    if index < len(subplot_titles):
        ax.set_title(subplot_titles[index])
    elif auto:
        ax.set_title(auto)


def _apply_geo_axis_labels(ax, xlabel, ylabel, *, xlabel_on=True, ylabel_on=True):
    """Lon/lat names; matplotlib places them relative to the colorbar slot."""
    xlab = _resolve_axis_label(xlabel, "Longitude")
    ylab = _resolve_axis_label(ylabel, "Latitude")
    ax.set_xlabel(xlab if xlabel_on else "")
    ax.set_ylabel(ylab if ylabel_on else "")


def _wind_component_role(da):
    """``'u'`` / ``'v'`` from CF ``standard_name``, or None."""
    sn = da.attrs.get("standard_name")
    if not isinstance(sn, str) or not sn.strip():
        return None
    key = sn.strip().lower()
    if "eastward" in key and "wind" in key:
        return "u"
    if "northward" in key and "wind" in key:
        return "v"
    return None


def _infer_uv_partner(name, *, want_v):
    """Guess the complementary u/v variable name, or None."""
    pairs = dict(_UV_NAME_PAIRS)
    inv = {v: u for u, v in _UV_NAME_PAIRS}
    if want_v:
        if name in pairs:
            return pairs[name]
        swapped = name.replace("eastward", "northward").replace("u_component", "v_component")
        if swapped != name:
            return swapped
        if name.startswith("u"):
            return "v" + name[1:]
        return None
    if name in inv:
        return inv[name]
    swapped = name.replace("northward", "eastward").replace("v_component", "u_component")
    if swapped != name:
        return swapped
    if name.startswith("v"):
        return "u" + name[1:]
    return None


def _resolve_uv(ds, u_variable, v_variable):
    """Eastward/northward variable names from flags, CF attrs, or common names."""
    names = list(ds.data_vars)
    if u_variable and u_variable not in ds:
        raise UsageError(f"--u-variable {u_variable!r} is not in the data (have {names})")
    if v_variable and v_variable not in ds:
        raise UsageError(f"--v-variable {v_variable!r} is not in the data (have {names})")
    if u_variable and v_variable:
        return u_variable, v_variable
    if u_variable:
        partner = _infer_uv_partner(u_variable, want_v=True)
        if partner and partner in ds:
            return u_variable, partner
        raise UsageError(
            f"--u-variable {u_variable!r} is set but no northward partner was found; "
            "pass --v-variable"
        )
    if v_variable:
        partner = _infer_uv_partner(v_variable, want_v=False)
        if partner and partner in ds:
            return partner, v_variable
        raise UsageError(
            f"--v-variable {v_variable!r} is set but no eastward partner was found; "
            "pass --u-variable"
        )
    u_cf, v_cf = [], []
    for name in names:
        role = _wind_component_role(ds[name])
        if role == "u":
            u_cf.append(name)
        elif role == "v":
            v_cf.append(name)
    if len(u_cf) == 1 and len(v_cf) == 1:
        return u_cf[0], v_cf[0]
    present = set(names)
    matches = [(u, v) for u, v in _UV_NAME_PAIRS if u in present and v in present]
    if matches:
        return matches[0]
    raise UsageError(
        "u/v plot needs eastward (u) and northward (v) wind components; "
        f"could not auto-detect them in {names}. Pass --u-variable and --v-variable."
    )


def _is_sample_dim(da, dim):
    """True if ``dim`` is flattened into wind-rose samples rather than indexed."""
    if dim in _SAMPLE_DIM_NAMES:
        return True
    for cf_name in ("latitude", "longitude", "time"):
        if cf_dim(da, cf_name) == dim:
            return True
    return False


def _flat_numeric(da):
    """Raveled float samples, stripping a pint wrapper if present."""
    import numpy as np

    if getattr(da.pint, "units", None) is not None:
        da = da.pint.dequantify()
    return np.asarray(da.values, dtype=float).reshape(-1)


def _uv_to_speed_fromdir(u, v):
    """Speed and meteorological FROM direction in degrees (0=N, 90=E)."""
    import numpy as np

    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    speed = np.hypot(u, v)
    fromdir = (np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0
    return speed, fromdir


def _speed_units_display(da):
    raw = variable_units(da)
    if not raw:
        return "m/s"
    if units_equal(raw, "m s-1"):
        return "m/s"
    return raw


def _speed_edges(speed, units):
    """Speed-bin edges. Standard 2 m/s classes when units are m/s, else 6 linear bins."""
    import numpy as np

    vmax = float(np.nanmax(speed)) if speed.size else 0.0
    ms = bool(units) and units_equal(units, "m s-1")
    if ms:
        return np.asarray([*WIND_SPEED_EDGES_MS, np.inf], dtype=float)
    if not np.isfinite(vmax) or vmax <= 0:
        return np.array([0.0, 1.0], dtype=float)
    return np.linspace(0.0, vmax, 7)


def _speed_bin_labels(edges):
    import numpy as np

    labels = []
    n = len(edges) - 1
    for i in range(n):
        lo = float(edges[i])
        hi = edges[i + 1]
        if np.isinf(hi):
            labels.append(f"≥{lo:g}")
        else:
            labels.append(f"{lo:g}–{hi:g}")
    return labels


def _speed_colors(n, colormap):
    import numpy as np
    from matplotlib import colormaps
    from matplotlib.colors import LinearSegmentedColormap

    if n < 1:
        return []
    if colormap is None:
        cmap = LinearSegmentedColormap.from_list("windrose", WIND_SPEED_COLORS)
    else:
        parsed = _parse_colormap(colormap)
        cmap = colormaps[parsed] if isinstance(parsed, str) else parsed
    if n == 1:
        return [cmap(0.5)]
    return [cmap(x) for x in np.linspace(0.0, 1.0, n)]


def _wind_rose_hist(speed, direction, speed_edges, nsector=WIND_ROSE_SECTORS):
    """2D histogram ``(nsector, nspeed)``. Sector 0 is North-centered."""
    import numpy as np

    offset = 180.0 / nsector
    shifted = (np.asarray(direction, dtype=float) + offset) % 360.0
    dir_edges = np.linspace(0.0, 360.0, nsector + 1)
    hist, _, _ = np.histogram2d(shifted, speed, bins=[dir_edges, speed_edges])
    return hist


def _windrose(
    speed,
    direction,
    *,
    title,
    fontsize,
    units_disp,
    colormap,
    units,
    figsize=None,
    legend=None,
    ylabel=None,
    mpl_spec=None,
):
    """Polar stacked-bar wind rose; radial axis is frequency percent."""
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch

    wr = windrose_kwargs(mpl_spec or {})
    nsector = int(wr.pop("nsector", WIND_ROSE_SECTORS))
    theta_zero = wr.pop("theta_zero_location", "N")
    theta_dir = wr.pop("theta_direction", -1)
    edgecolor = wr.pop("edgecolor", "white")
    linewidth = wr.pop("linewidth", 0.4)
    zorder = wr.pop("zorder", 2)
    speed_edges = _speed_edges(speed, units)
    hist = _wind_rose_hist(speed, direction, speed_edges, nsector=nsector)
    while hist.shape[1] > 1 and float(hist[:, -1].sum()) == 0:
        hist = hist[:, :-1]
        speed_edges = speed_edges[:-1]
    total = float(hist.sum())
    if total <= 0:
        raise UsageError("windrose has no finite u/v samples to plot.")
    freq = 100.0 * hist / total
    n_speed = freq.shape[1]
    colors = _speed_colors(n_speed, colormap)
    unit_suffix = f" {units_disp}" if units_disp else ""
    legend_labels = [f"{lab}{unit_suffix}" for lab in _speed_bin_labels(speed_edges)]
    width = 2.0 * np.pi / nsector
    theta = np.arange(nsector) * width
    fig = plt.figure(figsize=resolve_figsize(figsize, (8.5, 7.0)), layout="constrained")
    ax = fig.add_subplot(111, projection="polar")
    ax.set_theta_zero_location(str(theta_zero))
    ax.set_theta_direction(theta_dir)
    bottom = np.zeros(nsector)
    for i in range(n_speed):
        ax.bar(
            theta,
            freq[:, i],
            width=width,
            bottom=bottom,
            color=colors[i],
            edgecolor=edgecolor,
            linewidth=linewidth,
            align="center",
            zorder=zorder,
        )
        bottom += freq[:, i]
    ax.set_thetagrids(
        [0, 45, 90, 135, 180, 225, 270, 315],
        ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
    )
    ax.set_ylim(0, max(float(bottom.max()) * 1.08, 1.0))
    ax.set_ylabel(_resolve_axis_label(ylabel, "Frequency (%)"))
    handles = [
        Patch(facecolor=colors[i], edgecolor="white", label=legend_labels[i])
        for i in range(n_speed)
    ]
    _place_legend(ax, legend, default="outside right", handles=handles, title="Wind speed")
    if title:
        fig.suptitle(title)
    return fig


def _plot_windrose(
    ds,
    u_variable,
    v_variable,
    variable,
    overrides,
    bbox_nwse,
    mask_geojson,
    title,
    fontsize,
    colormap,
    figsize=None,
    legend=None,
    ylabel=None,
    mpl_spec=None,
):
    """Flatten u/v samples into one meteorological-from wind rose."""
    import numpy as np

    if variable:
        print(
            "Warning: --variable is ignored for --style windrose; "
            "use --u-variable/--v-variable or auto-detection.",
            file=sys.stderr,
        )
    u_name, v_name = _resolve_uv(ds, u_variable, v_variable)
    ds = to_standard_units(ds, variables=[u_name, v_name])
    u_da = ds[u_name]
    v_da = ds[v_name]
    u_da = _apply_index(u_da, overrides, list_dims=None)
    v_da = _apply_index(v_da, overrides, list_dims=None)
    extra = [d for d in u_da.dims if not _is_sample_dim(u_da, d)]
    if extra:
        raise UsageError(
            f"dimension {extra[0]!r} remains after selection; windrose "
            "flattens space/time/ensemble into samples — select a position "
            f"from {extra[0]!r} with --index"
        )
    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    if bbox_nwse is not None or region_polygon is not None:
        lat_dim = cf_dim(u_da, "latitude")
        lon_dim = cf_dim(u_da, "longitude")
        if lat_dim and lon_dim and lat_dim in u_da.dims and lon_dim in u_da.dims:
            u_da, _ = _subset_spatial(u_da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
            v_da, _ = _subset_spatial(v_da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
        else:
            u_da = _subset_points(u_da, bbox_nwse, region_polygon)
            v_da = _subset_points(v_da, bbox_nwse, region_polygon)
    u_vals = _flat_numeric(u_da)
    v_vals = _flat_numeric(v_da)
    if u_vals.size != v_vals.size:
        raise UsageError(
            f"u {u_name!r} and v {v_name!r} have different sizes after selection "
            f"({u_vals.size} vs {v_vals.size}); they must share coordinates"
        )
    valid = np.isfinite(u_vals) & np.isfinite(v_vals)
    u_vals, v_vals = u_vals[valid], v_vals[valid]
    if u_vals.size == 0:
        raise UsageError("windrose has no finite u/v samples to plot.")
    u_units = variable_units(u_da)
    v_units = variable_units(v_da)
    if u_units and v_units and not units_equal(u_units, v_units):
        raise UsageError(f"u units {u_units!r} do not match v units {v_units!r}")
    speed, direction = _uv_to_speed_fromdir(u_vals, v_vals)
    return _windrose(
        speed,
        direction,
        title=title,
        fontsize=fontsize,
        units_disp=_speed_units_display(u_da),
        colormap=colormap,
        units=u_units,
        figsize=figsize,
        legend=legend,
        ylabel=ylabel,
        mpl_spec=mpl_spec,
    )


def _wind_speed_da(u_da, v_da):
    """Speed from eastward/northward components, with a Wind speed label."""
    import numpy as np
    import xarray as xr

    u_da = _plain(u_da)
    v_da = _plain(v_da)
    speed = xr.apply_ufunc(np.hypot, u_da, v_da, keep_attrs=False)
    units = variable_units(u_da) or "m s-1"
    speed.name = "speed"
    speed.attrs.update(long_name="Wind speed", units=units, standard_name="wind_speed")
    return speed


def _wind_speed_cbar_label(u_da):
    units_disp = _speed_units_display(u_da)
    blob = " ".join(str(u_da.attrs.get(key) or "") for key in ("long_name", "GRIB_name")).lower()
    if "anomal" in blob:
        return f"Wind speed anomaly [{units_disp}]"
    return f"Wind speed [{units_disp}]"


def _mean_axis_spacing(values, axis):
    """Mean absolute spacing along one axis of a 1-D or 2-D coordinate."""
    import numpy as np

    values = np.asarray(values, dtype=float)
    if values.ndim == 0 or values.shape[axis] < 2:
        return None
    delta = np.diff(values, axis=axis)
    delta = delta[np.isfinite(delta)]
    if delta.size == 0:
        return None
    return float(np.mean(np.abs(delta)))


def _native_spacing_deg(lat, lon):
    """Finest mean lat/lon spacing in degrees, or None if it cannot be measured."""
    import numpy as np

    lat = np.asarray(lat)
    lon = np.asarray(lon)
    if lat.ndim == 1 and lon.ndim == 1:
        spacings = [_mean_axis_spacing(lat, 0), _mean_axis_spacing(lon, 0)]
    else:
        spacings = [
            _mean_axis_spacing(lat, 0),
            _mean_axis_spacing(lon, 1 if lon.ndim > 1 else 0),
        ]
    candidates = [s for s in spacings if s is not None and s > 0]
    return min(candidates) if candidates else None


def _quiver_step(lat, lon, requested=None, target_spacing=QUIVER_TARGET_SPACING_DEG):
    """Stride for quiver arrows.

    ``plot_wind_and_sst_anomaly`` uses ``quiver_step=1`` on the native S2S
    ~1.5° grid. When ``requested`` is set, use that. Otherwise thin finer
    grids (GFS 0.25°, ERA5) to about 1.5° so basin maps match that look.
    """
    if requested is not None:
        if requested < 1:
            raise UsageError("--quiver-step must be >= 1")
        return int(requested)
    spacing = _native_spacing_deg(lat, lon)
    if spacing is None:
        return QUIVER_STEP
    return max(QUIVER_STEP, int(round(target_spacing / spacing)))


def _auto_quiver_scale(u, v, lon_span, spacing_deg, requested=None):
    """Matplotlib quiver ``scale`` (data units per axes-width).

    Larger scale → shorter arrows. ``requested`` (``--quiver-scale``) wins.
    Otherwise size a typical (95th-percentile) wind to about
    ``QUIVER_ARROW_LEN_SPACING`` times the subsampled grid spacing, as a
    fraction of the map width, so 10 m/s basin winds and small anomalies
    both stay readable.
    """
    if requested is not None:
        if requested <= 0:
            raise UsageError("--quiver-scale must be > 0")
        return float(requested)
    import numpy as np

    speed = np.hypot(np.asarray(u, dtype=float), np.asarray(v, dtype=float))
    speed = speed[np.isfinite(speed)]
    if speed.size == 0 or lon_span <= 0 or spacing_deg is None or spacing_deg <= 0:
        return QUIVER_SCALE
    typical = float(np.percentile(speed, 95))
    if typical <= 0:
        return QUIVER_SCALE
    target_deg = QUIVER_ARROW_LEN_SPACING * float(spacing_deg)
    return typical * float(lon_span) / target_deg


def _subsample_quiver(lon, lat, u, v, step):
    """Native-grid u/v subsample, matching plot_wind_and_sst_anomaly."""
    import numpy as np

    lon = np.asarray(lon)
    lat = np.asarray(lat)
    u = np.asarray(u)
    v = np.asarray(v)
    if lon.ndim == 1 and lat.ndim == 1:
        lon, lat = np.meshgrid(lon, lat)
    step = max(1, int(step))
    return lon[::step, ::step], lat[::step, ::step], u[::step, ::step], v[::step, ::step]


def _quiver_map(
    speed,
    u_da,
    v_da,
    lat_dim,
    lon_dim,
    cmap,
    extent,
    cities,
    title,
    fontsize,
    wrap_lon=True,
    native_step_dim=None,
    native_steps=None,
    draw_boxes=None,
    rows=None,
    columns=None,
    quiver_scale=None,
    quiver_step=None,
    cbar_label=None,
    xlabel=None,
    ylabel=None,
    figsize=None,
    vmin=None,
    vmax=None,
    subplot_titles=None,
    mpl_spec=None,
):
    """Speed pcolormesh with native-grid u/v arrows (plot_wind_and_sst_anomaly)."""
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np

    def _prep(da):
        da = _plain(da)
        if "number" in da.dims:
            da = da.mean("number", keep_attrs=True)
        if wrap_lon:
            da = ensure_normalized_longitude(da, lon_dim)
        return da

    speed = _prep(speed)
    u_da = _prep(u_da)
    v_da = _prep(v_da)

    sdim = _step_dim(speed)
    if sdim is None or speed.sizes.get(sdim, 1) == 1:
        if sdim and sdim in speed.dims:
            speed = speed.squeeze(sdim, drop=True)
            u_da = u_da.squeeze(sdim, drop=True)
            v_da = v_da.squeeze(sdim, drop=True)
        steps = [None]
        sdim = None
    else:
        steps = list(speed[sdim].values)

    title_steps = native_steps if native_steps is not None and native_step_dim == sdim else steps
    num_steps = len(steps)
    subplot_titles = _resolve_subplot_titles(subplot_titles, num_steps)
    nrows, ncols = _panel_shape(num_steps, rows=rows, columns=columns)

    if extent is None:
        extent = _extent_from_field(speed, lat_dim, lon_dim)

    vmin, vmax, _ = _resolve_color_limits(speed, vmin, vmax)

    sw, sh = _figsize_from_extent(*extent)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=resolve_figsize(figsize, (sw * ncols, sh * nrows)),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
        layout="compressed",
    )
    axes = np.array(axes).reshape(nrows, ncols).flatten()

    mesh = None
    quiv = None
    boxes = draw_boxes or []
    overlays = _load_geo_overlays(extent)
    step = _quiver_step(speed[lat_dim].values, speed[lon_dim].values, quiver_step)
    native_spacing = _native_spacing_deg(speed[lat_dim].values, speed[lon_dim].values)
    arrow_spacing = None if native_spacing is None else native_spacing * step
    lon_span = abs(extent[1] - extent[0])
    scale = _auto_quiver_scale(
        u_da.values, v_da.values, lon_span, arrow_spacing, requested=quiver_scale
    )
    for i, s in enumerate(steps):
        ax = axes[i]
        slab = speed if sdim is None else speed.isel({sdim: i})
        u_slab = u_da if sdim is None else u_da.isel({sdim: i})
        v_slab = v_da if sdim is None else v_da.isel({sdim: i})
        slab = slab.transpose(lat_dim, lon_dim)
        u_slab = u_slab.transpose(lat_dim, lon_dim)
        v_slab = v_slab.transpose(lat_dim, lon_dim)
        if wrap_lon:
            ax.set_extent(extent, crs=ccrs.PlateCarree())
        else:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        mesh = ax.pcolormesh(
            slab[lon_dim],
            slab[lat_dim],
            slab.values,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            transform=ccrs.PlateCarree(),
            **mesh_kwargs(mpl_spec or {}),
        )
        lon_q, lat_q, u_q, v_q = _subsample_quiver(
            u_slab[lon_dim].values,
            u_slab[lat_dim].values,
            u_slab.values,
            v_slab.values,
            step,
        )
        q_kw = {
            "transform": ccrs.PlateCarree(),
            "scale": scale,
            "color": "k",
            "zorder": 5,
            **quiver_kwargs(mpl_spec or {}),
        }
        quiv = ax.quiver(
            lon_q,
            lat_q,
            u_q,
            v_q,
            **q_kw,
        )
        _draw_geo_overlays(ax, overlays, ccrs.PlateCarree())
        ax.gridlines(draw_labels=False, alpha=0)
        _apply_geo_axis_labels(
            ax,
            xlabel,
            ylabel,
            xlabel_on=(i // ncols == nrows - 1),
            ylabel_on=(i % ncols == 0),
        )
        for city, (lat, lon) in cities.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=ccrs.PlateCarree())
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                transform=ccrs.PlateCarree(),
            )
        if boxes:
            _draw_boxes_on_ax(ax, boxes, ccrs.PlateCarree())
        auto = _panel_title(speed, sdim, s, title_steps) if s is not None else None
        _set_panel_title(ax, i, auto, subplot_titles)

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    last = axes[num_steps - 1]
    units_disp = _speed_units_display(u_da)
    y_key = 0.18
    for u_ref in QUIVER_KEY_MS:
        last.quiverkey(
            quiv,
            1.18,
            y_key,
            u_ref,
            f"{u_ref:g} {units_disp}",
            labelpos="E",
            coordinates="axes",
        )
        y_key -= 0.10

    if title:
        fig.suptitle(title)
    visible = [ax for ax in axes if ax.get_visible()]
    cbar_kw = {}
    extend = _cbar_extend_for_limits(speed, vmin, vmax)
    if extend:
        cbar_kw["extend"] = extend
    add_shared_colorbar(fig, mesh, visible, cbar_label or _wind_speed_cbar_label(u_da), **cbar_kw)
    return fig


def _plot_quiver(
    ds,
    u_variable,
    v_variable,
    variable,
    overrides,
    bbox_nwse,
    mask_geojson,
    extent,
    cities,
    title,
    fontsize,
    colormap,
    draw_boxes,
    rows,
    columns,
    quiver_scale,
    quiver_step,
    xlabel=None,
    ylabel=None,
    figsize=None,
    vmin=None,
    vmax=None,
    subplot_titles=None,
    cbar_label=None,
    mpl_spec=None,
):
    """Map panels of wind speed with S2S-style u/v quiver overlay."""
    if variable:
        print(
            "Warning: --variable is ignored for --style quiver; "
            "use --u-variable/--v-variable or auto-detection.",
            file=sys.stderr,
        )
    u_name, v_name = _resolve_uv(ds, u_variable, v_variable)
    ds = to_standard_units(ds, variables=[u_name, v_name])
    u_da = ds[u_name]
    v_da = ds[v_name]
    u_units = variable_units(u_da)
    v_units = variable_units(v_da)
    if u_units and v_units and not units_equal(u_units, v_units):
        raise UsageError(f"u units {u_units!r} do not match v units {v_units!r}")
    u_da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps = (
        _prepare_gridded_map(u_da, overrides, bbox_nwse, mask_geojson, extent, style="quiver")
    )
    v_da, *_ = _prepare_gridded_map(
        v_da, overrides, bbox_nwse, mask_geojson, extent, style="quiver"
    )
    speed = _wind_speed_da(u_da, v_da)
    cmap = _parse_colormap(colormap) if colormap else QUIVER_CMAP
    return _quiver_map(
        speed,
        u_da,
        v_da,
        lat_dim,
        lon_dim,
        cmap,
        extent_vals,
        _parse_cities(cities),
        title,
        fontsize,
        wrap_lon=wrap_lon,
        native_step_dim=native_step_dim,
        native_steps=native_steps,
        draw_boxes=draw_boxes,
        rows=rows,
        columns=columns,
        quiver_scale=quiver_scale,
        quiver_step=quiver_step,
        cbar_label=cbar_label or _wind_speed_cbar_label(u_da),
        xlabel=xlabel,
        ylabel=ylabel,
        figsize=figsize,
        vmin=vmin,
        vmax=vmax,
        subplot_titles=subplot_titles,
        mpl_spec=mpl_spec,
    )


def _label_key(value):
    import numpy as np

    arr = np.asarray(value)
    if arr.dtype.kind in ("M", "m"):
        return int(arr.astype("int64"))
    obj = arr.item() if getattr(arr, "shape", ()) == () else value
    if hasattr(obj, "calendar"):
        return (obj.calendar, str(obj))
    return obj


def _point_dim(ds):
    for name in ("station_id", "point_id"):
        if name in ds.dims:
            return name
    return None


def _combined_mask_polygon(mask_geojson, layers):
    paths = []
    flags = []
    if mask_geojson:
        paths.append(mask_geojson)
        flags.append("--mask-geojson")
    for spec in layers:
        if spec.kind == "mask":
            paths.append(spec.path)
            flags.append("--layer mask")
    if not paths:
        return None
    from shapely.ops import unary_union

    geoms = [polygon_from_geojson(path, flag=flag) for path, flag in zip(paths, flags, strict=True)]
    return geoms[0] if len(geoms) == 1 else unary_union(geoms)


def _layer_overrides(spec, default_index):
    raw = spec.options.get("index", default_index)
    if not raw:
        return {}
    try:
        return _parse_index(raw)
    except ValueError as exc:
        raise UsageError(f"--layer {spec.kind}:{spec.path}: {exc}") from None


def _copy_layer(spec, options=None):
    out = LayerSpec(
        spec.kind, spec.path, options if options is not None else spec.options, spec.raw
    )
    out.ds = spec.ds
    return out


def _ensure_layer_dataset(spec):
    if spec.kind not in _ZARR_LAYER_KINDS:
        return
    if spec.ds is not None:
        return
    import xarray as xr

    if not spec.path.exists():
        raise UsageError(f"input not found: {spec.path}")
    spec.ds = xr.open_zarr(spec.path, consolidated=True)


def _layer_variable(ds, spec):
    variable = spec.options.get("variable") or auto_variable(ds)
    if not variable or variable not in ds:
        raise UsageError(
            f"--layer {spec.kind}:{spec.path}: no usable variable. Available: {list(ds.data_vars)}"
        )
    return variable


def _common_labels(driver_values, other_values, spec):
    other_keys = {_label_key(v) for v in other_values}
    common = [v for v in driver_values if _label_key(v) in other_keys]
    if not common:
        raise UsageError(
            f"no overlapping time bins between the panel axis and --layer {spec.kind}:{spec.path}; "
            "aggregate both inputs to a common resolution first, e.g. with the "
            "aggregate-temporal skill"
        )
    return common


def _align_panel_labels(driver_dim, driver_values, driver_kind, da, spec):
    """Return ``(panel_dim or None, labels or None)`` for this layer vs the driver."""
    other_dim = _step_dim(da)
    if other_dim is None:
        return None, None
    other_values = list(da[other_dim].values)
    other_kind = _axis_kind(da[other_dim].values)
    if driver_kind != other_kind or driver_kind is None or other_kind is None:
        driver_name = "forecast step" if driver_kind == "timedelta" else "calendar time"
        other_name = "forecast step" if other_kind == "timedelta" else "calendar time"
        if driver_kind == "timedelta" or other_kind == "timedelta":
            raise UsageError(
                f"--layer {spec.kind}:{spec.path} has a {other_name} axis ({other_dim!r}) but the "
                f"panel axis is a {driver_name} axis ({driver_dim!r}). Run the step-to-time skill "
                "on the forecast before overlaying observations."
            )
        raise UsageError(
            f"--layer {spec.kind}:{spec.path} time axis {other_dim!r} is not comparable to "
            f"panel axis {driver_dim!r}"
        )
    return other_dim, _common_labels(driver_values, other_values, spec)


def _extent_from_field(da, lat_dim, lon_dim):
    return _pad_cell_extent(da[lat_dim].values, da[lon_dim].values)


def _extent_from_points(da):
    import numpy as np

    lat_name = cf_dim(da, "latitude")
    lon_name = cf_dim(da, "longitude")
    lats = np.asarray(da[lat_name].values, dtype=float)
    lons = np.asarray(da[lon_name].values, dtype=float)
    lats = lats[np.isfinite(lats)]
    lons = lons[np.isfinite(lons)]
    if lats.size == 0 or lons.size == 0:
        raise UsageError("scatter layer has no finite lat/lon coordinates")
    pad = 0.5
    return [
        float(lons.min()) - pad,
        float(lons.max()) + pad,
        float(lats.min()) - pad,
        float(lats.max()) + pad,
    ]


def _prep_heatmap_layer(spec, bbox_nwse, region_polygon, extent):
    _ensure_layer_dataset(spec)
    ds = spec.ds
    variable = _layer_variable(ds, spec)
    ds = to_standard_units(ds, variables=[variable])
    ds = precip_for_display(ds, variable)
    da = ds[variable]
    overrides = _layer_overrides(spec, spec.options.get("index"))
    da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps = (
        _prepare_gridded_map(
            da,
            overrides,
            bbox_nwse,
            None,
            extent,
            style="heatmap",
            region_polygon=region_polygon,
        )
    )
    if wrap_lon:
        da = ensure_normalized_longitude(da, lon_dim)
    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)
    user_vmin = _layer_optional_float(spec, "vmin")
    user_vmax = _layer_optional_float(spec, "vmax")
    user_vlim = user_vmin is not None or user_vmax is not None
    flag_scale = _discrete_flag_scale(da, spec.options.get("colormap"))
    if flag_scale is not None:
        if user_vlim:
            raise UsageError("--vmin/--vmax cannot be used with CF flag_values fields")
        cmap, norm, flag_ticks, flag_labels = flag_scale
        vmin = vmax = None
    else:
        cmap, norm = _heatmap_scale(da, spec.options.get("colormap"), stretch=user_vlim)
        flag_ticks = flag_labels = None
        vmin, vmax, norm = _resolve_color_limits(da, user_vmin, user_vmax, norm=norm)
    return {
        "kind": "heatmap",
        "spec": spec,
        "da": da,
        "lat_dim": lat_dim,
        "lon_dim": lon_dim,
        "cmap": cmap,
        "norm": norm,
        "vmin": vmin,
        "vmax": vmax,
        "vlim_user": user_vlim,
        "flag_ticks": flag_ticks,
        "flag_labels": flag_labels,
        "wrap_lon": wrap_lon,
        "native_step_dim": native_step_dim,
        "native_steps": native_steps,
        "panel_dim": _step_dim(da),
        "cbar_label": _variable_label(da),
        "variable": variable,
        "units": variable_units(da),
        "zorder": _KIND_ZORDER["heatmap"],
    }


def _prep_scatter_layer(spec, bbox_nwse, region_polygon):
    _ensure_layer_dataset(spec)
    ds = spec.ds
    point_dim = _point_dim(ds)
    if point_dim is None:
        raise UsageError(
            f"--layer scatter:{spec.path} needs a station_id or point_id dimension "
            f"(got dims {list(ds.dims)})"
        )
    variable = _layer_variable(ds, spec)
    ds = to_standard_units(ds, variables=[variable])
    ds = precip_for_display(ds, variable)
    da = ds[variable]
    overrides = _layer_overrides(spec, spec.options.get("index"))
    panel_dim = _step_dim(da)
    da = _apply_index(da, overrides, list_dims=(panel_dim,) if panel_dim else ())
    if bbox_nwse is not None or region_polygon is not None:
        da = _subset_points(da, bbox_nwse, region_polygon)
    extra = [d for d in da.dims if d not in (panel_dim, point_dim) and d is not None]
    extra = [d for d in extra if d in da.dims]
    if extra:
        if extra == ["number"] or (len(extra) == 1 and extra[0] == "number"):
            da = da.mean("number", keep_attrs=True)
        else:
            raise UsageError(
                f"--layer scatter:{spec.path} still has dimension(s) {extra}; "
                "select a position with index= or reduce them first"
            )
    user_vmin = _layer_optional_float(spec, "vmin")
    user_vmax = _layer_optional_float(spec, "vmax")
    user_vlim = user_vmin is not None or user_vmax is not None
    cmap, norm = _heatmap_scale(da, spec.options.get("colormap"), stretch=user_vlim)
    vmin, vmax, norm = _resolve_color_limits(da, user_vmin, user_vmax, norm=norm)
    return {
        "kind": "scatter",
        "spec": spec,
        "da": da,
        "ds": ds,
        "point_dim": point_dim,
        "cmap": cmap,
        "norm": norm,
        "vmin": vmin,
        "vmax": vmax,
        "vlim_user": user_vlim,
        "panel_dim": _step_dim(da),
        "cbar_label": _variable_label(da),
        "variable": variable,
        "units": variable_units(da),
        "zorder": _KIND_ZORDER["scatter"],
    }


def _prep_quiver_layer(spec, bbox_nwse, region_polygon, extent):
    _ensure_layer_dataset(spec)
    ds = spec.ds
    u_name, v_name = _resolve_uv(ds, spec.options.get("u-variable"), spec.options.get("v-variable"))
    ds = to_standard_units(ds, variables=[u_name, v_name])
    u_da = ds[u_name]
    v_da = ds[v_name]
    u_units = variable_units(u_da)
    v_units = variable_units(v_da)
    if u_units and v_units and not units_equal(u_units, v_units):
        raise UsageError(f"u units {u_units!r} do not match v units {v_units!r}")
    overrides = _layer_overrides(spec, spec.options.get("index"))
    u_da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps = (
        _prepare_gridded_map(
            u_da,
            overrides,
            bbox_nwse,
            None,
            extent,
            style="quiver",
            region_polygon=region_polygon,
        )
    )
    v_da, *_ = _prepare_gridded_map(
        v_da,
        overrides,
        bbox_nwse,
        None,
        extent,
        style="quiver",
        region_polygon=region_polygon,
    )
    if wrap_lon:
        u_da = ensure_normalized_longitude(u_da, lon_dim)
        v_da = ensure_normalized_longitude(v_da, lon_dim)
    if "number" in u_da.dims:
        u_da = u_da.mean("number", keep_attrs=True)
        v_da = v_da.mean("number", keep_attrs=True)
    speed = _wind_speed_da(u_da, v_da)
    cmap = (
        _parse_colormap(spec.options.get("colormap"))
        if spec.options.get("colormap")
        else QUIVER_CMAP
    )
    qscale = spec.options.get("quiver-scale")
    qstep = spec.options.get("quiver-step")
    user_vmin = _layer_optional_float(spec, "vmin")
    user_vmax = _layer_optional_float(spec, "vmax")
    user_vlim = user_vmin is not None or user_vmax is not None
    vmin, vmax, _ = _resolve_color_limits(speed, user_vmin, user_vmax)
    return {
        "kind": "quiver",
        "spec": spec,
        "speed": speed,
        "u_da": u_da,
        "v_da": v_da,
        "lat_dim": lat_dim,
        "lon_dim": lon_dim,
        "cmap": cmap,
        "norm": None,
        "vmin": vmin,
        "vmax": vmax,
        "vlim_user": user_vlim,
        "wrap_lon": wrap_lon,
        "native_step_dim": native_step_dim,
        "native_steps": native_steps,
        "panel_dim": _step_dim(speed),
        "cbar_label": _wind_speed_cbar_label(u_da),
        "variable": "speed",
        "units": variable_units(u_da),
        "quiver_scale": float(qscale) if qscale is not None else None,
        "quiver_step": int(qstep) if qstep is not None else None,
        "zorder": _KIND_ZORDER["quiver"],
        "draw_mesh": False,
    }


def _prep_outline_layer(spec):
    return {
        "kind": "outline",
        "spec": spec,
        "polygon": polygon_from_geojson(spec.path, flag="--layer outline"),
        "panel_dim": None,
        "zorder": _KIND_ZORDER["outline"],
    }


def _layer_field(prepared):
    if prepared["kind"] == "quiver":
        return prepared.get("speed")
    return prepared.get("da")


def _sel_layer(prepared, dim, labels):
    if prepared["kind"] == "heatmap":
        prepared["da"] = prepared["da"].sel({dim: labels})
    elif prepared["kind"] == "scatter":
        prepared["da"] = prepared["da"].sel({dim: labels})
    elif prepared["kind"] == "quiver":
        prepared["speed"] = prepared["speed"].sel({dim: labels})
        prepared["u_da"] = prepared["u_da"].sel({dim: labels})
        prepared["v_da"] = prepared["v_da"].sel({dim: labels})


def _squeeze_layer_dim(prepared, dim):
    field = _layer_field(prepared)
    if field is None or dim not in getattr(field, "dims", ()):
        return
    if field.sizes[dim] != 1:
        return
    if prepared["kind"] == "heatmap":
        prepared["da"] = prepared["da"].squeeze(dim, drop=True)
    elif prepared["kind"] == "scatter":
        prepared["da"] = prepared["da"].squeeze(dim, drop=True)
    elif prepared["kind"] == "quiver":
        prepared["speed"] = prepared["speed"].squeeze(dim, drop=True)
        prepared["u_da"] = prepared["u_da"].squeeze(dim, drop=True)
        prepared["v_da"] = prepared["v_da"].squeeze(dim, drop=True)
    prepared["panel_dim"] = None


def _select_panel(prepared, label):
    """Return a copy of ``prepared`` reduced to one panel label, or the original if static."""
    dim = prepared.get("panel_dim")
    if dim is None or label is None:
        return prepared
    out = dict(prepared)
    if prepared["kind"] == "heatmap":
        out["da"] = prepared["da"].sel({dim: label})
    elif prepared["kind"] == "scatter":
        out["da"] = prepared["da"].sel({dim: label})
    elif prepared["kind"] == "quiver":
        out["speed"] = prepared["speed"].sel({dim: label})
        out["u_da"] = prepared["u_da"].sel({dim: label})
        out["v_da"] = prepared["v_da"].sel({dim: label})
    out["panel_dim"] = None
    return out


def _draw_heatmap_on_ax(ax, prepared, transform):
    da = _plain(prepared["da"])
    lat_dim, lon_dim = prepared["lat_dim"], prepared["lon_dim"]
    slab = da.transpose(lat_dim, lon_dim)
    return ax.pcolormesh(
        slab[lon_dim],
        slab[lat_dim],
        slab.values,
        cmap=prepared["cmap"],
        norm=prepared["norm"],
        vmin=prepared["vmin"],
        vmax=prepared["vmax"],
        transform=transform,
        zorder=prepared["zorder"],
    )


def _draw_scatter_on_ax(ax, prepared, transform):
    da = _plain(prepared["da"])
    lat_name = cf_dim(da, "latitude")
    lon_name = cf_dim(da, "longitude")
    return ax.scatter(
        da[lon_name].values,
        da[lat_name].values,
        c=da.values,
        cmap=prepared["cmap"],
        norm=prepared["norm"],
        vmin=prepared["vmin"],
        vmax=prepared["vmax"],
        s=30,
        transform=transform,
        zorder=prepared["zorder"],
        edgecolors="k",
        linewidths=0.3,
    )


def _draw_quiver_on_ax(ax, prepared, transform, scale, step, mpl_spec=None):
    u_da = _plain(prepared["u_da"])
    v_da = _plain(prepared["v_da"])
    lat_dim, lon_dim = prepared["lat_dim"], prepared["lon_dim"]
    u_slab = u_da.transpose(lat_dim, lon_dim)
    v_slab = v_da.transpose(lat_dim, lon_dim)
    mesh = None
    if prepared.get("draw_mesh"):
        speed = _plain(prepared["speed"]).transpose(lat_dim, lon_dim)
        mesh = ax.pcolormesh(
            speed[lon_dim],
            speed[lat_dim],
            speed.values,
            cmap=prepared["cmap"],
            vmin=prepared["vmin"],
            vmax=prepared["vmax"],
            transform=transform,
            zorder=1.0,
            **mesh_kwargs(mpl_spec or {}),
        )
    lon_q, lat_q, u_q, v_q = _subsample_quiver(
        u_slab[lon_dim].values,
        u_slab[lat_dim].values,
        u_slab.values,
        v_slab.values,
        step,
    )
    quiv = ax.quiver(
        lon_q,
        lat_q,
        u_q,
        v_q,
        **{
            "transform": transform,
            "scale": scale,
            "color": "k",
            "zorder": prepared["zorder"],
            **quiver_kwargs(mpl_spec or {}),
        },
    )
    return mesh, quiv


def _draw_outline_on_ax(ax, prepared, crs):
    ax.add_geometries(
        [prepared["polygon"]],
        crs,
        facecolor="none",
        edgecolor="black",
        linewidth=1.2,
        zorder=prepared["zorder"],
    )


def _scale_groups(prepared_layers, shared_scale, independent_scale):
    """Return True if heatmap/scatter layers should share one color scale."""
    data = [p for p in prepared_layers if p["kind"] in ("heatmap", "scatter")]
    if len(data) < 2:
        return False
    if independent_scale:
        return False
    if shared_scale:
        return True
    variables = {p["variable"] for p in data}
    units = {p["units"] for p in data if p["units"]}
    return len(variables) == 1 and len(units) <= 1


def _apply_shared_scale(prepared_layers):
    data = [p for p in prepared_layers if p["kind"] in ("heatmap", "scatter")]
    if not data:
        return
    user = [p for p in data if p.get("vlim_user")]
    if user:
        limits = {(p["vmin"], p["vmax"]) for p in user}
        if len(limits) > 1:
            raise UsageError(
                "shared-scale layers disagree on vmin/vmax; "
                "use --independent-scale or one set of limits"
            )
        cmap, norm = user[0]["cmap"], user[0]["norm"]
        vmin, vmax = user[0]["vmin"], user[0]["vmax"]
    else:
        cmap, norm = data[0]["cmap"], data[0]["norm"]
        if norm is None:
            vmins = [p["vmin"] for p in data if p["vmin"] is not None]
            vmaxs = [p["vmax"] for p in data if p["vmax"] is not None]
            vmin = min(vmins) if vmins else None
            vmax = max(vmaxs) if vmaxs else None
            if vmin is not None and vmax is not None and vmax > 0 and vmin < 0:
                m = max(abs(vmax), abs(vmin))
                vmin, vmax = -m, m
        else:
            vmin = vmax = None
    for p in data:
        p["cmap"] = cmap
        p["norm"] = norm
        p["vmin"] = vmin
        p["vmax"] = vmax


def _plot_layers(
    layers,
    bbox_nwse,
    mask_geojson,
    extent,
    cities,
    title,
    fontsize,
    draw_boxes,
    rows,
    columns,
    variable,
    colormap,
    index,
    u_variable,
    v_variable,
    quiver_scale,
    quiver_step,
    shared_scale,
    independent_scale,
    layer_labels=None,
    xlabel=None,
    ylabel=None,
    figsize=None,
    vmin=None,
    vmax=None,
    subplot_titles=None,
    cbar_label=None,
    mpl_spec=None,
):
    """Stack ``--layer`` entries on shared Cartopy panels."""
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np

    if shared_scale and independent_scale:
        raise UsageError("--shared-scale and --independent-scale are mutually exclusive")

    label_slots = resolve_input_labels(layer_labels, len(layers), input_flag="--layer")

    inherited = []
    for spec in layers:
        opts = dict(spec.options)
        if "variable" not in opts and variable:
            opts["variable"] = variable
        if "colormap" not in opts and colormap:
            opts["colormap"] = colormap
        if "index" not in opts and index:
            opts["index"] = index
        if "u-variable" not in opts and u_variable:
            opts["u-variable"] = u_variable
        if "v-variable" not in opts and v_variable:
            opts["v-variable"] = v_variable
        if "quiver-scale" not in opts and quiver_scale is not None:
            opts["quiver-scale"] = str(quiver_scale)
        if "quiver-step" not in opts and quiver_step is not None:
            opts["quiver-step"] = str(quiver_step)
        if "vmin" not in opts and vmin is not None:
            opts["vmin"] = str(vmin)
        if "vmax" not in opts and vmax is not None:
            opts["vmax"] = str(vmax)
        inherited.append(_copy_layer(spec, opts))

    region_polygon = _combined_mask_polygon(mask_geojson, inherited)
    extent_vals = _parse_extent(extent)
    prepared = []
    for i, spec in enumerate(inherited):
        if spec.kind == "mask":
            continue
        if spec.kind == "heatmap":
            item = _prep_heatmap_layer(spec, bbox_nwse, region_polygon, extent)
        elif spec.kind == "scatter":
            item = _prep_scatter_layer(spec, bbox_nwse, region_polygon)
        elif spec.kind == "quiver":
            item = _prep_quiver_layer(spec, bbox_nwse, region_polygon, extent)
        elif spec.kind == "outline":
            item = _prep_outline_layer(spec)
        else:
            raise UsageError(f"unknown --layer kind {spec.kind!r}")
        label_override = label_slots[i]
        if label_override:
            item["cbar_label"] = label_override
        elif cbar_label:
            item["cbar_label"] = cbar_label
        elif spec.ds is not None and spec.kind in {"heatmap", "scatter", "quiver"}:
            item["cbar_label"] = dataset_display_label(spec.ds, item.get("cbar_label") or spec.path)
        item["zorder"] = item["zorder"] + i * 0.01
        prepared.append(item)

    if not prepared:
        raise UsageError("--layer needs at least one heatmap, scatter, quiver, or outline")

    has_heatmap = any(p["kind"] == "heatmap" for p in prepared)
    for p in prepared:
        if p["kind"] == "quiver":
            p["draw_mesh"] = not has_heatmap

    driver = next((p for p in prepared if p.get("panel_dim")), None)
    if driver is None:
        steps = [None]
        sdim = None
        title_da = None
        title_steps = [None]
    else:
        sdim = driver["panel_dim"]
        title_da = _layer_field(driver)
        driver_values = list(title_da[sdim].values)
        driver_kind = _axis_kind(title_da[sdim].values)
        aligned = driver_values
        for p in prepared:
            if p is driver:
                continue
            field = _layer_field(p)
            if field is None:
                continue
            other_dim, labels = _align_panel_labels(sdim, aligned, driver_kind, field, p["spec"])
            if other_dim is None:
                continue
            aligned = labels
            p["panel_dim"] = other_dim
        if not aligned:
            raise UsageError("no overlapping time bins across --layer inputs")
        for p in prepared:
            field = _layer_field(p)
            dim = p.get("panel_dim")
            if field is None or dim is None or dim not in field.dims:
                continue
            _sel_layer(p, dim, aligned)
        title_da = _layer_field(driver)
        steps = list(title_da[sdim].values) if sdim in title_da.dims else aligned
        if sdim is not None and title_da.sizes.get(sdim, 1) == 1:
            for p in prepared:
                _squeeze_layer_dim(p, p.get("panel_dim"))
            steps = [None]
            sdim = None
        native = driver.get("native_steps")
        native_dim = driver.get("native_step_dim")
        title_steps = native if native is not None and native_dim == sdim else steps

    for p in prepared:
        dim = p.get("panel_dim")
        field = _layer_field(p)
        if sdim is None and dim and field is not None and dim in field.dims:
            raise UsageError(
                f"--layer {p['spec'].kind}:{p['spec'].path} still has {dim!r}; "
                "select a position with index= (the other layers have no panel axis)"
            )

    if extent_vals is None:
        if bbox_nwse is not None:
            r_n, r_w, r_s, r_e = bbox_nwse
            extent_vals = [float(r_w), float(r_e), float(r_s), float(r_n)]
        else:
            extent_vals = None
            for p in prepared:
                if p["kind"] in ("heatmap", "quiver"):
                    src = p["da"] if p["kind"] == "heatmap" else p["speed"]
                    extent_vals = _extent_from_field(src, p["lat_dim"], p["lon_dim"])
                    break
            if extent_vals is None:
                scatter = next((p for p in prepared if p["kind"] == "scatter"), None)
                if scatter is not None:
                    extent_vals = _extent_from_points(scatter["da"])
                else:
                    raise UsageError("could not determine map extent; pass --extent or --bbox")

    wrap_lon = True
    for p in prepared:
        if "wrap_lon" in p:
            wrap_lon = p["wrap_lon"]
            break

    share = _scale_groups(prepared, shared_scale, independent_scale)
    if share:
        _apply_shared_scale(prepared)

    num_steps = len(steps)
    subplot_titles = _resolve_subplot_titles(subplot_titles, num_steps)
    nrows, ncols = _panel_shape(num_steps, rows=rows, columns=columns)
    sw, sh = _figsize_from_extent(*extent_vals)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=resolve_figsize(figsize, (sw * ncols, sh * nrows)),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
        layout="compressed",
    )
    axes = np.array(axes).reshape(nrows, ncols).flatten()
    overlays = _load_geo_overlays(extent_vals)
    cities_map = _parse_cities(cities)
    boxes = draw_boxes or []
    transform = ccrs.PlateCarree()

    quiver_meta = None
    for p in prepared:
        if p["kind"] == "quiver":
            step = _quiver_step(
                p["u_da"][p["lat_dim"]].values, p["u_da"][p["lon_dim"]].values, p.get("quiver_step")
            )
            native_spacing = _native_spacing_deg(
                p["u_da"][p["lat_dim"]].values, p["u_da"][p["lon_dim"]].values
            )
            arrow_spacing = None if native_spacing is None else native_spacing * step
            lon_span = abs(extent_vals[1] - extent_vals[0])
            scale = _auto_quiver_scale(
                p["u_da"].values,
                p["v_da"].values,
                lon_span,
                arrow_spacing,
                requested=p.get("quiver_scale"),
            )
            quiver_meta = (p, scale, step)
            break

    last_by_group = {}
    last_quiv = None
    for i, s in enumerate(steps):
        ax = axes[i]
        if wrap_lon:
            ax.set_extent(extent_vals, crs=transform)
        else:
            ax.set_xlim(extent_vals[0], extent_vals[1])
            ax.set_ylim(extent_vals[2], extent_vals[3])
        for p in prepared:
            slab = _select_panel(p, s)
            if slab["kind"] == "heatmap":
                last_by_group.setdefault(id(p) if not share else "shared", None)
                artist = _draw_heatmap_on_ax(ax, slab, transform)
                last_by_group["shared" if share else id(p)] = (artist, p)
            elif slab["kind"] == "scatter":
                artist = _draw_scatter_on_ax(ax, slab, transform)
                last_by_group["shared" if share else id(p)] = (artist, p)
            elif slab["kind"] == "quiver":
                _, scale, step = quiver_meta
                mesh, quiv = _draw_quiver_on_ax(ax, slab, transform, scale, step, mpl_spec=mpl_spec)
                last_quiv = quiv
                if mesh is not None:
                    last_by_group[id(p)] = (mesh, p)
            elif slab["kind"] == "outline":
                _draw_outline_on_ax(ax, slab, transform)
        _draw_geo_overlays(ax, overlays, transform)
        ax.gridlines(draw_labels=False, alpha=0)
        _apply_geo_axis_labels(
            ax,
            xlabel,
            ylabel,
            xlabel_on=(i // ncols == nrows - 1),
            ylabel_on=(i % ncols == 0),
        )
        for city, (lat, lon) in cities_map.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=transform, zorder=8)
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                transform=transform,
                zorder=8,
            )
        if boxes:
            _draw_boxes_on_ax(ax, boxes, transform)
        auto = (
            _panel_title(title_da, sdim, s, title_steps)
            if s is not None and title_da is not None
            else None
        )
        _set_panel_title(ax, i, auto, subplot_titles)

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    if last_quiv is not None:
        last = axes[num_steps - 1]
        qlayer = next(p for p in prepared if p["kind"] == "quiver")
        units_disp = _speed_units_display(qlayer["u_da"])
        y_key = 0.18
        for u_ref in QUIVER_KEY_MS:
            last.quiverkey(
                last_quiv,
                1.18,
                y_key,
                u_ref,
                f"{u_ref:g} {units_disp}",
                labelpos="E",
                coordinates="axes",
            )
            y_key -= 0.10

    if title:
        fig.suptitle(title)

    visible = [ax for ax in axes if ax.get_visible()]
    for mappable, p in last_by_group.values():
        kw = dict(_cbar_boundary_kwargs(p.get("norm"), p.get("cmap")))
        if p.get("flag_ticks") is not None:
            kw["ticks"] = p["flag_ticks"]
        field = p.get("da") if p.get("da") is not None else p.get("speed")
        extend = None
        if field is not None:
            extend = _cbar_extend_for_limits(field, p.get("vmin"), p.get("vmax"))
        if extend and "extend" not in kw:
            kw["extend"] = extend
        cbar = add_shared_colorbar(
            fig,
            mappable,
            visible,
            p.get("cbar_label") or _variable_label(p.get("da")),
            **kw,
        )
        if cbar is not None and p.get("flag_labels") is not None:
            cbar.set_ticklabels(p["flag_labels"])
    return fig


def _contour_levels(vmin, vmax, n=10, norm=None):
    """Shared isoline edges for every contour panel (and a constant-field pad)."""
    import numpy as np

    boundaries = getattr(norm, "boundaries", None) if norm is not None else None
    if boundaries is not None:
        return list(boundaries)
    if vmin is None or vmax is None or not np.isfinite(vmin) or not np.isfinite(vmax):
        return n
    if vmin == vmax:
        pad = abs(vmin) * 0.05 if vmin != 0 else 1.0
        return np.linspace(vmin - pad, vmax + pad, n + 1)
    return np.linspace(vmin, vmax, n + 1)


def _heatmap(
    da,
    lat_dim,
    lon_dim,
    cmap,
    extent,
    cities,
    title,
    fontsize,
    wrap_lon=True,
    native_step_dim=None,
    native_steps=None,
    draw_boxes=None,
    norm=None,
    flag_ticks=None,
    flag_labels=None,
    rows=None,
    columns=None,
    kind="heatmap",
    xlabel=None,
    ylabel=None,
    figsize=None,
    vmin=None,
    vmax=None,
    subplot_titles=None,
    cbar_label=None,
):
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np

    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)

    if wrap_lon:
        da = ensure_normalized_longitude(da, lon_dim)
    if kind == "contour":
        lat_vals = np.asarray(da[lat_dim].values)
        if lat_vals.size > 1 and float(lat_vals[0]) > float(lat_vals[-1]):
            da = da.sortby(lat_dim)

    sdim = _step_dim(da)
    if sdim is None or da.sizes.get(sdim, 1) == 1:
        if sdim and sdim in da.dims:
            da = da.squeeze(sdim, drop=True)
        steps = [None]
        sdim = None
    else:
        steps = list(da[sdim].values)

    title_steps = native_steps if native_steps is not None and native_step_dim == sdim else steps

    num_steps = len(steps)
    subplot_titles = _resolve_subplot_titles(subplot_titles, num_steps)
    nrows, ncols = _panel_shape(num_steps, rows=rows, columns=columns)

    if extent is None:
        extent = _extent_from_field(da, lat_dim, lon_dim)

    vmin, vmax, norm = _resolve_color_limits(da, vmin, vmax, norm=norm)

    sw, sh = _figsize_from_extent(*extent)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=resolve_figsize(figsize, (sw * ncols, sh * nrows)),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
        layout="compressed",
    )
    axes = np.array(axes).reshape(nrows, ncols).flatten()

    mappable = None
    boxes = draw_boxes or []
    overlays = _load_geo_overlays(extent)
    levels = _contour_levels(vmin, vmax, norm=norm) if kind == "contour" else None
    contour_extend = "both" if getattr(cmap, "name", None) in DISCRETE_PRECIP_NAMES else "neither"
    for i, s in enumerate(steps):
        ax = axes[i]
        slab = da if sdim is None else da.isel({sdim: i})
        slab = slab.transpose(lat_dim, lon_dim)
        lon_1d = slab[lon_dim]
        lat_1d = slab[lat_dim]
        if kind == "contour":
            mappable = ax.contourf(
                lon_1d,
                lat_1d,
                slab.values,
                levels=levels,
                cmap=cmap,
                norm=norm,
                transform=ccrs.PlateCarree(),
                extend=contour_extend,
            )
            ax.contour(
                lon_1d,
                lat_1d,
                slab.values,
                levels=levels,
                colors="k",
                linewidths=0.4,
                transform=ccrs.PlateCarree(),
            )
        else:
            mappable = ax.pcolormesh(
                lon_1d,
                lat_1d,
                slab.values,
                cmap=cmap,
                norm=norm,
                vmin=vmin,
                vmax=vmax,
                transform=ccrs.PlateCarree(),
            )
        if wrap_lon:
            ax.set_extent(extent, crs=ccrs.PlateCarree())
        else:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        _draw_geo_overlays(ax, overlays, ccrs.PlateCarree())
        ax.gridlines(draw_labels=False, alpha=0)
        _apply_geo_axis_labels(
            ax,
            xlabel,
            ylabel,
            xlabel_on=(i // ncols == nrows - 1),
            ylabel_on=(i % ncols == 0),
        )
        for city, (lat, lon) in cities.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=ccrs.PlateCarree())
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                transform=ccrs.PlateCarree(),
            )
        if boxes:
            _draw_boxes_on_ax(ax, boxes, ccrs.PlateCarree())
        auto = _panel_title(da, sdim, s, title_steps) if s is not None else None
        _set_panel_title(ax, i, auto, subplot_titles)

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    if title:
        fig.suptitle(title)
    visible = [ax for ax in axes if ax.get_visible()]
    cbar_kw = dict(_cbar_boundary_kwargs(norm, cmap))
    if flag_ticks is not None:
        cbar_kw["ticks"] = flag_ticks
    extend = _cbar_extend_for_limits(da, vmin, vmax)
    if extend and "extend" not in cbar_kw:
        cbar_kw["extend"] = extend
    cbar = add_shared_colorbar(fig, mappable, visible, cbar_label or _variable_label(da), **cbar_kw)
    if cbar is not None and flag_labels is not None:
        cbar.set_ticklabels(flag_labels)
    return fig


_SPEC_STYLES = frozenset({"heatmap", "timeseries", "contour"})


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


def _first_spec_input(spec_data):
    for item in spec_data.get("inputs") or []:
        if isinstance(item, dict):
            return item
    return {}


def _overlay_cli_from_spec(spec_data, **cli):
    """Fill omitted CLI flags from a dumped spec. CLI values win when set."""
    geo = spec_data.get("geo") or {}
    traces = spec_data.get("traces") or []
    traces0 = traces[0] if traces and isinstance(traces[0], dict) else {}
    inp0 = _first_spec_input(spec_data)
    out = dict(cli)
    if out.get("title") is None:
        layout_title = (spec_data.get("layout") or {}).get("title")
        if isinstance(layout_title, dict):
            out["title"] = layout_title.get("text") or spec_data.get("title")
        elif isinstance(layout_title, str):
            out["title"] = layout_title
        else:
            out["title"] = spec_data.get("title")
    for key in ("xlabel", "ylabel", "cbar_label", "colormap", "legend", "index"):
        if out.get(key) is None and spec_data.get(key) is not None:
            out[key] = spec_data[key]
    if out.get("index") is None and inp0.get("index") is not None:
        out["index"] = inp0["index"]
    if out.get("u_variable") is None:
        out["u_variable"] = (
            spec_data.get("u_variable")
            or traces0.get("u_variable")
            or traces0.get("u-variable")
            or inp0.get("u_variable")
            or inp0.get("u-variable")
        )
    if out.get("v_variable") is None:
        out["v_variable"] = (
            spec_data.get("v_variable")
            or traces0.get("v_variable")
            or traces0.get("v-variable")
            or inp0.get("v_variable")
            or inp0.get("v-variable")
        )
    if out.get("variable") is None:
        out["variable"] = traces0.get("variable") or inp0.get("variable")
    if out.get("bbox") is None and geo.get("bbox") is not None:
        out["bbox"] = tuple(geo["bbox"])
    if not out.get("mask_geojson") and geo.get("mask_geojson"):
        out["mask_geojson"] = geo["mask_geojson"]
    if out.get("extent") is None and geo.get("extent") is not None:
        out["extent"] = geo["extent"]
    if not out.get("cities") and geo.get("cities"):
        out["cities"] = geo["cities"]
    if not out.get("draw_box") and geo.get("draw_boxes"):
        out["draw_box"] = geo["draw_boxes"]
    if out.get("figsize") is None and (spec_data.get("layout") or {}).get("figsize"):
        fig = (spec_data.get("layout") or {}).get("figsize")
        out["figsize"] = tuple(fig) if isinstance(fig, (list, tuple)) else fig
    if out.get("vmin") is None and spec_data.get("vmin") is not None:
        out["vmin"] = spec_data["vmin"]
    if out.get("vmax") is None and spec_data.get("vmax") is not None:
        out["vmax"] = spec_data["vmax"]
    return out


def _style_from_spec(spec_data):
    if spec_data.get("layers") or spec_data.get("layered"):
        return None
    traces = spec_data.get("traces") or []
    if traces and isinstance(traces[0], dict):
        kind = traces[0].get("type")
        if kind and kind not in {"layer", "layers"}:
            return kind
    return None


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
    pair_on = pair_on or trace.get("pair_on") or spec_data.get("pair_on") or "time"
    inputs = [i for i in (spec_data.get("inputs") or []) if isinstance(i, dict)]
    x_variable = (
        x_variable
        or trace.get("x_variable")
        or spec_data.get("x_variable")
        or (inputs[0].get("x_variable") if inputs else None)
        or (inputs[0].get("variable") if inputs else None)
    )
    y_variable = (
        y_variable
        or trace.get("y_variable")
        or spec_data.get("y_variable")
        or (inputs[0].get("y_variable") if inputs else None)
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


def _layer_datasets(layers):
    named = {}
    for layer in layers:
        if layer.kind not in _ZARR_LAYER_KINDS:
            continue
        ds = layer.ds
        if ds is None:
            continue
        key = chr(ord("a") + len(named))
        named[key] = ds
    return named


def _layer_spec_entries(layers, named):
    path_to_id = {}
    for key, ds in named.items():
        raw = _input_path_of(ds)
        if raw:
            path_to_id[str(Path(raw))] = key
    entries = []
    for layer in layers:
        item = {
            "kind": layer.kind,
            "path": str(layer.path),
            "options": dict(layer.options),
        }
        if layer.kind in _ZARR_LAYER_KINDS:
            item["input"] = path_to_id.get(str(layer.path)) or path_to_id.get(str(Path(layer.path)))
        entries.append(item)
    return entries


def _resolved_drawn_spec(
    *,
    style,
    datasets,
    spec_data,
    title,
    xlabel,
    ylabel,
    cbar_label,
    figsize,
    colormap,
    fontsize,
    theme,
    layers=None,
    pair_on=None,
    x_variable=None,
    y_variable=None,
    u_variable=None,
    v_variable=None,
    legend=None,
    vmin=None,
    vmax=None,
    bbox_nwse=None,
    extent=None,
    cities=None,
    mask_geojson=None,
    draw_boxes=None,
):
    resolved = overlay_spec(
        {"version": 1, "layout": {}, "style": {}, "geo": {}, "annotations": [], "shapes": []},
        spec_data or {},
    )
    if datasets:
        resolved["inputs"] = spec_inputs_from_datasets(datasets)
        if style == "xy" and resolved["inputs"]:
            if x_variable:
                resolved["inputs"][0]["variable"] = x_variable
                resolved["inputs"][0]["x_variable"] = x_variable
            if y_variable and len(resolved["inputs"]) > 1:
                resolved["inputs"][1]["variable"] = y_variable
            elif y_variable:
                resolved["inputs"][0]["y_variable"] = y_variable
    if layers:
        resolved["traces"] = [{"type": "layer"}]
        resolved["layers"] = _layer_spec_entries(layers, datasets or {})
        resolved["layered"] = True
    elif style == "xy":
        ids = list(datasets.keys()) if isinstance(datasets, dict) else []
        trace = {"type": "xy", "pair_on": pair_on or "time"}
        if x_variable:
            trace["x_variable"] = x_variable
        if y_variable:
            trace["y_variable"] = y_variable
        if len(ids) >= 2:
            trace["x"] = ids[0]
            trace["y"] = ids[1]
        elif ids:
            trace["input"] = ids[0]
        resolved["traces"] = [trace]
    else:
        resolved["traces"] = [{"type": style, "input": "a"}]
        if u_variable:
            resolved["u_variable"] = u_variable
        if v_variable:
            resolved["v_variable"] = v_variable
    if title is not None:
        resolved["title"] = title
    if xlabel is not None:
        resolved["xlabel"] = xlabel
    if ylabel is not None:
        resolved["ylabel"] = ylabel
    if cbar_label is not None:
        resolved["cbar_label"] = cbar_label
    if legend is not None:
        resolved["legend"] = legend
    if vmin is not None:
        resolved["vmin"] = vmin
    if vmax is not None:
        resolved["vmax"] = vmax
    if colormap:
        resolved.setdefault("style", {})["colormap"] = colormap
    if fontsize is not None:
        resolved.setdefault("style", {})["fontsize"] = fontsize
    if theme:
        resolved.setdefault("style", {})["template"] = theme
    if figsize is not None:
        resolved.setdefault("layout", {})["figsize"] = list(figsize)
        resolved["layout"]["autosize"] = False
    if extent is not None:
        resolved.setdefault("geo", {})["extent"] = extent
    if cities:
        resolved.setdefault("geo", {})["cities"] = cities
    if bbox_nwse is not None:
        resolved.setdefault("geo", {})["bbox"] = list(bbox_nwse)
    if mask_geojson:
        resolved.setdefault("geo", {})["mask_geojson"] = str(mask_geojson)
    if draw_boxes:
        resolved.setdefault("geo", {})["draw_boxes"] = list(draw_boxes)
    return resolved


def _export_drawn_figure(fig, resolved, output, *, datasets, dump_spec, spec_data):
    from weather_skills_core.plot_export import write_plot_outputs

    finish_figure(fig, spec_data or resolved)
    return write_plot_outputs(
        fig,
        resolved,
        output,
        datasets=datasets,
        dump_spec_path=dump_spec_dest(dump_spec),
        spec=spec_data,
    )


def _render_spec_plot(
    ds,
    spec,
    *,
    variable,
    style,
    colormap,
    title,
    subplot_title,
    xlabel,
    ylabel,
    cbar_label,
    index,
    extent,
    cities,
    fontsize,
    figsize,
    legend,
    mask_geojson,
    draw_boxes,
    rows,
    columns,
    bbox_nwse,
    vmin,
    vmax,
    style_file,
    patch,
    dump_spec_path,
    output,
    theme=None,
):
    """Compile heatmap/timeseries/contour and write PNG + spec sidecar."""
    from weather_skills_core.plot_compile import compile_figure
    from weather_skills_core.plot_export import write_plot_outputs
    from weather_skills_core.plot_style import deep_merge

    user_style = load_user_style(style_file)
    template = theme or user_style.get("template")
    input_path = _input_path_of(ds)
    if spec is not None:
        merged = overlay_spec(spec.to_dict() if isinstance(spec, PlotSpec) else spec, {})
        if input_path:
            inputs = list(merged.get("inputs") or [])
            if inputs:
                inputs[0] = {**inputs[0], "path": inputs[0].get("path") or input_path}
                if variable:
                    inputs[0]["variable"] = variable
                if index:
                    inputs[0]["index"] = index
            else:
                inputs = [{"id": "a", "path": input_path, "variable": variable, "index": index}]
            merged["inputs"] = inputs
        if title is not None:
            merged["title"] = title
        if colormap is not None:
            merged.setdefault("style", {})["colormap"] = colormap
        if fontsize is not None:
            merged.setdefault("style", {})["fontsize"] = fontsize
        if template:
            merged.setdefault("style", {})["template"] = template
        if subplot_title:
            merged["subplot_titles"] = list(subplot_title)
        if xlabel is not None:
            merged["xlabel"] = xlabel
        if ylabel is not None:
            merged["ylabel"] = ylabel
        if cbar_label is not None:
            merged["cbar_label"] = cbar_label
        if legend is not None:
            merged["legend"] = legend
        if vmin is not None:
            merged["vmin"] = vmin
        if vmax is not None:
            merged["vmax"] = vmax
        if figsize is not None:
            merged.setdefault("layout", {})["figsize"] = list(figsize)
            merged["layout"]["autosize"] = False
        if extent is not None:
            merged.setdefault("geo", {})["extent"] = extent
        if cities:
            merged.setdefault("geo", {})["cities"] = cities
        if bbox_nwse is not None:
            merged.setdefault("geo", {})["bbox"] = list(bbox_nwse)
        if mask_geojson:
            merged.setdefault("geo", {})["mask_geojson"] = str(mask_geojson)
        if draw_boxes:
            merged.setdefault("geo", {})["draw_boxes"] = list(draw_boxes)
        if rows is not None:
            merged.setdefault("layout", {}).setdefault("facet", {})["rows"] = rows
        if columns is not None:
            merged.setdefault("layout", {}).setdefault("facet", {})["columns"] = columns
        if patch:
            merged["patch"] = deep_merge(merged.get("patch") or {}, patch)
        if not merged.get("traces"):
            merged["traces"] = [{"type": style, "input": "a"}]
    else:
        merged = spec_from_flags(
            input_path=input_path,
            variable=variable,
            style=style,
            colormap=colormap or user_style.get("colormap"),
            title=title,
            subplot_titles=subplot_title,
            xlabel=xlabel,
            ylabel=ylabel,
            cbar_label=cbar_label,
            index=index,
            extent=extent,
            cities=cities,
            fontsize=fontsize if fontsize is not None else user_style.get("fontsize"),
            figsize=figsize,
            legend=legend,
            bbox=list(bbox_nwse) if bbox_nwse is not None else None,
            mask_geojson=mask_geojson,
            draw_boxes=draw_boxes,
            rows=rows,
            columns=columns,
            vmin=vmin,
            vmax=vmax,
            patch=patch,
            template=template,
        )
    if user_style.get("max_columns") and not (rows or columns):
        merged.setdefault("layout", {}).setdefault("facet", {}).setdefault(
            "max_columns", user_style["max_columns"]
        )

    datasets = {"a": ds}
    if spec is not None and spec.datasets:
        for i, extra in enumerate(spec.datasets):
            key = (
                (spec.data.get("inputs") or [{}])[i].get("id") if spec.data.get("inputs") else None
            )
            datasets[key or f"i{i}"] = extra
        datasets["a"] = ds

    fig, resolved = compile_figure(merged, datasets)
    spec_dest = dump_spec_dest(dump_spec_path)
    return write_plot_outputs(
        fig,
        resolved,
        output,
        datasets=datasets,
        dump_spec_path=spec_dest,
        spec=merged,
    )


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
    help="X-axis Zarr for --style xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--y",
    dest="y_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="Y-axis Zarr for --style xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--layer",
    action="append",
    default=None,
    type=parse_layer,
    help=(
        "Map layer KIND:PATH or KIND:PATH::k=v. Repeat for overlays. "
        "Kinds: heatmap, scatter, quiver, outline, mask. "
        "Options: variable, colormap, index, u-variable, v-variable, "
        "quiver-scale, quiver-step, vmin, vmax. Mutually exclusive with -i/--input."
    ),
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--x-variable",
    default=None,
    help="X-axis variable for --style xy. Defaults to the first data variable of --x (or -i).",
)
@weather_skill.argument(
    "--y-variable",
    default=None,
    help="Y-axis variable for --style xy. Defaults to the first data variable of --y (or -i).",
)
@weather_skill.argument(
    "--pair-on",
    choices=["time", "year", "index"],
    default=None,
    help=(
        "How --style xy matches --x to --y samples: shared time (default), "
        "calendar year (e.g. September IOD vs October rain), or position."
    ),
)
@weather_skill.argument(
    "--style",
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
        "Heatmap default: CHC ppt_total / ppt_anomaly classes for precip "
        "(aliases chirps_total, chirps_anom), else rocket. Also: ppt_poa, "
        "ppt_spp, spi. Windrose default: blue-to-orange speed classes. "
        "Quiver default: YlGn (ECMWF S2S 10 m / 700 hPa wind vectors)."
    ),
)
@weather_skill.argument(
    "--index",
    default=None,
    help=(
        "Slice like 'step=3,number=0' (heatmap, contour, quiver, and windrose). "
        "Heatmap/contour/quiver lists keep the dim as panels; windrose lists keep samples."
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
    help="Figure size W,H inches (e.g. 10,6 or 10x6). Default is style-specific.",
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
        "Default is the variable long_name + units. Per-layer --label wins."
    ),
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
        "Plot every Nth grid point for --style quiver "
        "(S2S plot_wind_and_sst_anomaly quiver_step). "
        "Default: 1 on ~1.5° grids; finer grids auto-thin to ~1.5°. Quiver-only."
    ),
)
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help="Colorbar label for each --layer, in order. Omit to infer from metadata.",
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
    "--style-file",
    default=None,
    help="User plot style TOML/JSON (colormap, fontsize, template). Overrides ~/.config/weather-skills/plot.toml.",
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
    type=parse_json_object,
    help=(
        "Partial figure update (title/annotations/shapes/colorbar) merged after compile. "
        'Colorbar size: {"layout": {"colorbar": {"len": 0.45, "thickness": 12}}}.'
    ),
)
@weather_skill.argument(
    "--dump-spec",
    default=None,
    help=DUMP_SPEC_ARGUMENT_HELP,
)
def plot(
    ds,
    bbox,
    variable,
    style,
    colormap,
    title,
    subplot_title,
    xlabel,
    ylabel,
    cbar_label,
    index,
    extent,
    cities,
    fontsize,
    figsize,
    legend,
    mask_geojson,
    draw_box,
    rows,
    columns,
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
    style_file=None,
    theme=None,
    patch=None,
    dump_spec=None,
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
    layers = list(layer or [])
    if not layers:
        layers = _layers_from_spec(spec_data, spec)
    if style is None and not layers:
        style = _style_from_spec(spec_data) or "heatmap"
    if spec is not None and ds is None and not layers and style != "xy":
        ds = spec.ds if spec.ds is not None else None
        if ds is None and spec.datasets:
            ds = spec.datasets[0]
    if layers and ds is not None:
        raise UsageError("pass either -i/--input or --layer, not both")
    if layers and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either --layer or --x/--y, not both")
    if ds is not None and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either -i/--input or --x/--y, not both")
    if style == "xy":
        x_ds, y_ds, x_variable, y_variable, pair_on = _xy_from_spec(
            spec_data, spec, x_ds, y_ds, x_variable, y_variable, pair_on
        )
        if layers:
            raise UsageError("--layer cannot be used with --style xy")
        if x_ds is None and y_ds is None:
            if ds is None:
                raise UsageError(
                    "--style xy needs --x and --y, or -i with --x-variable and --y-variable"
                )
            if not x_variable or not y_variable:
                raise UsageError(
                    "with a single -i, --style xy needs both --x-variable and --y-variable"
                )
            x_ds = ds
            y_ds = ds
        elif x_ds is None or y_ds is None:
            raise UsageError("--style xy needs both --x and --y")
    elif not layers and ds is None:
        raise UsageError("pass -i/--input, a --spec with inputs, or at least one --layer")
    if layers and style in ("timeseries", "xy", "windrose", "contour"):
        raise UsageError(f"--layer cannot be used with --style {style}")
    if layers and style == "quiver":
        raise UsageError(
            "with --layer, draw wind vectors as --layer quiver:PATH instead of --style quiver"
        )

    try:
        overrides = _parse_index(index)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    bbox_nwse = bbox
    draw_boxes = _parse_draw_boxes(draw_box)

    legend_used = legend is not None and legend != "none"
    if layers and legend_used:
        print(
            "Warning: --legend is ignored for layered maps (they use colorbars).",
            file=sys.stderr,
        )
    if not layers and style in _SPEC_STYLES:
        if style == "timeseries":
            for flag, set_ in {
                "--extent": bool(extent),
                "--cities": bool(cities),
                "--draw-box": bool(draw_boxes),
                "--rows": rows is not None,
                "--columns": columns is not None,
                "--subplot-title": bool(subplot_title),
                "--bbox": bbox_nwse is not None,
                "--mask-geojson": bool(mask_geojson),
                "--index": bool(overrides),
            }.items():
                if set_:
                    print(
                        f"Warning: {flag} is a map-only option; ignored for --style {style}.",
                        file=sys.stderr,
                    )
        elif legend_used:
            print(
                f"Warning: --legend is ignored for --style {style} (maps use a colorbar).",
                file=sys.stderr,
            )
        if style == "heatmap" and (u_variable or v_variable):
            print(
                "Warning: --u-variable/--v-variable is only used with --style windrose or "
                "--style quiver; ignored for --style heatmap.",
                file=sys.stderr,
            )
        return _render_spec_plot(
            ds,
            spec,
            variable=variable,
            style=style,
            colormap=colormap,
            title=title,
            subplot_title=subplot_title,
            xlabel=xlabel,
            ylabel=ylabel,
            cbar_label=cbar_label,
            index=index,
            extent=extent,
            cities=cities,
            fontsize=fontsize,
            figsize=figsize,
            legend=legend,
            mask_geojson=mask_geojson,
            draw_boxes=draw_boxes,
            rows=rows,
            columns=columns,
            bbox_nwse=bbox_nwse,
            vmin=vmin,
            vmax=vmax,
            style_file=style_file,
            theme=theme,
            patch=patch,
            dump_spec_path=dump_spec,
            output=output,
        )

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter

    apply_style(fontsize, template=theme or "weather_skills")
    apply_rc((spec_data.get("style") or {}).get("rc") or spec_data.get("rc"))

    if layers:
        fig = _plot_layers(
            layers,
            bbox_nwse,
            mask_geojson,
            extent,
            cities,
            title,
            fontsize,
            draw_boxes,
            rows,
            columns,
            variable,
            colormap,
            index,
            u_variable,
            v_variable,
            quiver_scale,
            quiver_step,
            shared_scale,
            independent_scale,
            layer_labels=label,
            xlabel=xlabel,
            ylabel=ylabel,
            figsize=figsize,
            vmin=vmin,
            vmax=vmax,
            subplot_titles=subplot_title,
            cbar_label=cbar_label,
            mpl_spec=spec_data,
        )
        named = _layer_datasets(layers)
        resolved = _resolved_drawn_spec(
            style="layer",
            datasets=named,
            spec_data=spec_data,
            title=title,
            xlabel=xlabel,
            ylabel=ylabel,
            cbar_label=cbar_label,
            figsize=figsize,
            colormap=colormap,
            fontsize=fontsize,
            theme=theme,
            layers=layers,
            vmin=vmin,
            vmax=vmax,
            bbox_nwse=bbox_nwse,
            extent=extent,
            cities=cities,
            mask_geojson=mask_geojson,
            draw_boxes=draw_boxes,
        )
        return _export_drawn_figure(
            fig,
            resolved,
            output,
            datasets=named,
            dump_spec=dump_spec,
            spec_data=spec_data,
        )
    map_only = {
        "--extent": bool(extent),
        "--cities": bool(cities),
        "--draw-box": bool(draw_boxes),
        "--rows": rows is not None,
        "--columns": columns is not None,
        "--subplot-title": bool(subplot_title),
    }
    uv_flags = {
        "--u-variable": bool(u_variable),
        "--v-variable": bool(v_variable),
    }
    quiver_only = {
        "--quiver-scale": quiver_scale is not None,
        "--quiver-step": quiver_step is not None,
    }
    vlim_flags = {
        "--vmin": vmin is not None,
        "--vmax": vmax is not None,
        "--cbar-label": bool(cbar_label),
    }

    def _flag_detail(flag):
        if flag == "--bbox" and bbox_nwse is not None:
            return f" {bbox_nwse[0]}/{bbox_nwse[1]}/{bbox_nwse[2]}/{bbox_nwse[3]}"
        if flag == "--extent":
            return f" {extent!r}"
        if flag == "--index":
            return f" {index!r}"
        if flag == "--draw-box":
            return f" {draw_box!r}"
        return ""

    if style == "xy":
        for flag, set_ in map_only.items():
            if set_:
                print(
                    f"Warning: {flag}{_flag_detail(flag)} is a map-only option; "
                    f"ignored for --style {style}.",
                    file=sys.stderr,
                )
        for flag, set_ in {**uv_flags, **quiver_only, **vlim_flags}.items():
            if set_:
                print(
                    f"Warning: {flag} is ignored for --style {style}.",
                    file=sys.stderr,
                )
        if variable:
            print(
                "Warning: --variable is ignored for --style xy; use --x-variable/--y-variable.",
                file=sys.stderr,
            )
        if legend_used:
            print(
                "Warning: --legend is ignored for --style xy.",
                file=sys.stderr,
            )
    elif style == "quiver":
        if legend_used:
            print(
                "Warning: --legend is ignored for --style quiver (maps use a colorbar).",
                file=sys.stderr,
            )
    elif style == "windrose":
        for flag, set_ in {**map_only, **quiver_only, **vlim_flags}.items():
            if set_:
                print(
                    f"Warning: {flag}{_flag_detail(flag)} is ignored for --style windrose.",
                    file=sys.stderr,
                )

    common = dict(
        spec_data=spec_data,
        title=title,
        xlabel=xlabel,
        ylabel=ylabel,
        cbar_label=cbar_label,
        figsize=figsize,
        colormap=colormap,
        fontsize=fontsize,
        theme=theme,
        legend=legend,
        vmin=vmin,
        vmax=vmax,
        bbox_nwse=bbox_nwse,
        extent=extent,
        cities=cities,
        mask_geojson=mask_geojson,
        draw_boxes=draw_boxes,
    )
    if style == "xy":
        fig = _plot_xy(
            x_ds,
            y_ds,
            x_variable,
            y_variable,
            pair_on,
            overrides,
            bbox_nwse,
            mask_geojson,
            title,
            xlabel,
            ylabel,
            fontsize,
            figsize=figsize,
        )
        named = {"a": x_ds} if x_ds is y_ds else {"x": x_ds, "y": y_ds}
        resolved = _resolved_drawn_spec(
            style="xy",
            datasets=named,
            pair_on=pair_on,
            x_variable=x_variable,
            y_variable=y_variable,
            **common,
        )
    elif style == "windrose":
        fig = _plot_windrose(
            ds,
            u_variable,
            v_variable,
            variable,
            overrides,
            bbox_nwse,
            mask_geojson,
            title,
            fontsize,
            colormap,
            figsize=figsize,
            legend=legend,
            ylabel=ylabel,
            mpl_spec=spec_data,
        )
        named = {"a": ds}
        resolved = _resolved_drawn_spec(
            style="windrose",
            datasets=named,
            u_variable=u_variable,
            v_variable=v_variable,
            **common,
        )
    elif style == "quiver":
        fig = _plot_quiver(
            ds,
            u_variable,
            v_variable,
            variable,
            overrides,
            bbox_nwse,
            mask_geojson,
            extent,
            cities,
            title,
            fontsize,
            colormap,
            draw_boxes,
            rows,
            columns,
            quiver_scale,
            quiver_step,
            xlabel=xlabel,
            ylabel=ylabel,
            figsize=figsize,
            vmin=vmin,
            vmax=vmax,
            subplot_titles=subplot_title,
            cbar_label=cbar_label,
            mpl_spec=spec_data,
        )
        named = {"a": ds}
        resolved = _resolved_drawn_spec(
            style="quiver",
            datasets=named,
            u_variable=u_variable,
            v_variable=v_variable,
            **common,
        )
    else:
        raise UsageError(f"unsupported plot style {style!r}")

    return _export_drawn_figure(
        fig,
        resolved,
        output,
        datasets=named,
        dump_spec=dump_spec,
        spec_data=spec_data,
    )


if __name__ == "__main__":
    plot()

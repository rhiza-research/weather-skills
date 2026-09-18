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
from weather_skills_core.plot.compile import (
    figsize_from_extent,
    panel_title,
    parse_cities,
    parse_draw_boxes,
    plain,
    step_dim,
    subset_spatial,
    timeseries_axis,
)
from weather_skills_core.plot.figure import (
    DEFAULT_FONTSIZE,
    add_shared_colorbar,
    apply_style,
    parse_figsize,
    resolve_axis_label,
    resolve_figsize,
)
from weather_skills_core.plot.geo import (
    draw_box_outlines,
    draw_geo_overlays,
    load_geo_overlays,
)
from weather_skills_core.plot.layers import (
    _SAMPLE_DIM_NAMES,
    _ZARR_LAYER_KINDS,
    QUIVER_CMAP,
    QUIVER_KEY_MS,
    WIND_ROSE_SECTORS,
    WIND_SPEED_COLORS,
    WIND_SPEED_EDGES_MS,
    LayerSpec,
    _apply_geo_axis_labels,
    _auto_quiver_scale,
    _cbar_extend_for_limits,
    _extent_from_field,
    _native_spacing_deg,
    _parse_colormap,
    _plot_layers,
    _prepare_gridded_map,
    _quiver_step,
    _resolve_color_limits,
    _resolve_subplot_titles,
    _resolve_uv,
    _set_panel_title,
    _speed_units_display,
    _subsample_quiver,
    _subset_points,
    _variable_label,
    _wind_speed_cbar_label,
    _wind_speed_da,
    parse_layer,
)
from weather_skills_core.plot.mpl import (
    apply_rc,
    finish_figure,
    mesh_kwargs,
    quiver_kwargs,
    windrose_kwargs,
)
from weather_skills_core.plot.spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    PlotSpec,
    apply_index,
    dump_spec_dest,
    named_datasets_from_spec,
    opened_datasets_from_spec,
    overlay_flags,
    overlay_spec,
    panel_shape,
    params_from_spec,
    parse_index,
    parse_plot_spec,
    patch_parser_for_spec_flags,
    spec_from_flags,
    spec_get,
    spec_input_labels,
    spec_inputs_from_datasets,
    trace_at,
)
from weather_skills_core.plot.style import (
    PRECIP_ANOMALY_BOUNDS,
    PRECIP_BOUNDS,
    PRECIP_SHORT_BOUNDS,
    load_user_style,
    set_active_style,
)
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    parse_bbox,
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


# Meteorological wind rose: 16 compass sectors, speed stacked in m/s classes.

# Speed field matches plot_s2s 10 m / 700 hPa (YlGn). Arrows sit on the
# native grid (plot_wind_and_sst_anomaly), thinned to ~1.5°. Scale is
# auto-picked so a typical wind is ~1.5× that spacing — a fixed 100 matches
# S2S *anomaly* magnitudes and overdraws 10 m/s basin winds.


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
    da = apply_index(plain(ds[variable]), overrides, list_dims=())
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if (bbox_nwse is not None or region_polygon is not None) and lat_dim and lon_dim:
        if lat_dim in da.dims and lon_dim in da.dims:
            da, _ = subset_spatial(da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
    sdim = "step" if "step" in da.dims else cf_dim(da, "time")
    if sdim is None:
        if da.ndim == 1:
            sdim = da.dims[0]
        else:
            raise UsageError(f"{role} needs a time/step axis to pair samples; got {list(da.dims)}.")
    reduce_dims = [d for d in da.dims if d != sdim]
    reduced = da.mean(reduce_dims, keep_attrs=True) if reduce_dims else da
    axis_vals, _ = timeseries_axis(reduced, sdim)
    values = np.asarray(plain(reduced).values, dtype=float)
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
    ax.set_xlabel(resolve_axis_label(xlabel, _variable_label(x_da)))
    ax.set_ylabel(resolve_axis_label(ylabel, _variable_label(y_da)))
    x_qty = variable_label_for_display(x_da, include_units=False)
    y_qty = variable_label_for_display(y_da, include_units=False)
    ax.set_title(title or f"{y_qty} vs {x_qty}")
    ax.grid(True, alpha=0.3)
    return fig


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

    wr = windrose_kwargs(trace_at(mpl_spec))
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
    ax.set_ylabel(resolve_axis_label(ylabel, "Frequency (%)"))
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
    u_da = apply_index(u_da, overrides, list_dims=None)
    v_da = apply_index(v_da, overrides, list_dims=None)
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
            u_da, _ = subset_spatial(u_da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
            v_da, _ = subset_spatial(v_da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
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
        da = plain(da)
        if "number" in da.dims:
            da = da.mean("number", keep_attrs=True)
        if wrap_lon:
            da = ensure_normalized_longitude(da, lon_dim)
        return da

    speed = _prep(speed)
    u_da = _prep(u_da)
    v_da = _prep(v_da)

    sdim = step_dim(speed)
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
    nrows, ncols = panel_shape(num_steps, rows=rows, columns=columns)

    if extent is None:
        extent = _extent_from_field(speed, lat_dim, lon_dim)

    vmin, vmax, _ = _resolve_color_limits(speed, vmin, vmax)

    sw, sh = figsize_from_extent(*extent)
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
    overlays = load_geo_overlays(extent)
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
            **mesh_kwargs(trace_at(mpl_spec)),
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
            **quiver_kwargs(trace_at(mpl_spec)),
        }
        quiv = ax.quiver(
            lon_q,
            lat_q,
            u_q,
            v_q,
            **q_kw,
        )
        draw_geo_overlays(ax, overlays, ccrs.PlateCarree())
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
            draw_box_outlines(ax, boxes, ccrs.PlateCarree())
        auto = panel_title(speed, sdim, s, title_steps) if s is not None else None
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
        parse_cities(cities),
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


def _coerce_spec_params(spec_data, user_style=None):
    """Read figure knobs from the spec (plus user-style defaults)."""
    user_style = user_style or {}
    p = params_from_spec(spec_data)
    bbox = p.get("bbox")
    if isinstance(bbox, str):
        bbox = parse_bbox(bbox)
    elif bbox is not None:
        bbox = tuple(bbox)
    figsize = p.get("figsize")
    if isinstance(figsize, str):
        figsize = parse_figsize(figsize)
    elif figsize is not None:
        figsize = tuple(figsize)
    legend = p.get("legend")
    if legend is not None:
        legend = parse_legend(legend)
    fontsize = p.get("fontsize")
    if fontsize is None:
        fontsize = user_style.get("fontsize", DEFAULT_FONTSIZE)
    reduce = p.get("reduce") or []
    if isinstance(reduce, str):
        reduce = [reduce]
    subplot_titles = p.get("subplot_titles")
    if subplot_titles is not None:
        subplot_titles = list(subplot_titles)
    shared = p.get("shared_colorscale")
    quiver = trace_at(spec_data).get("quiver") or {}
    if not isinstance(quiver, dict):
        quiver = {}
    scale = p.get("quiver_scale")
    if scale is None:
        scale = quiver.get("scale")
    step = p.get("quiver_step")
    if step is None:
        step = quiver.get("step")
    return {
        "title": p.get("title"),
        "xlabel": p.get("xlabel"),
        "ylabel": p.get("ylabel"),
        "cbar_label": p.get("cbar_label"),
        "colormap": p.get("colormap") or user_style.get("colormap"),
        "colormap_bounds": p.get("colormap_bounds"),
        "colormap_under": p.get("colormap_under"),
        "colormap_over": p.get("colormap_over"),
        "cbar_ticks": p.get("cbar_ticks"),
        "cbar_labels": p.get("cbar_labels"),
        "legend": legend,
        "index": p.get("index"),
        "u_variable": p.get("u_variable"),
        "v_variable": p.get("v_variable"),
        "variable": p.get("variable"),
        "bbox": bbox,
        "mask_geojson": p.get("mask_geojson"),
        "extent": p.get("extent"),
        "cities": p.get("cities"),
        "draw_box": p.get("draw_boxes"),
        "figsize": figsize,
        "vmin": p.get("vmin"),
        "vmax": p.get("vmax"),
        "fontsize": fontsize,
        "template": p.get("template") or user_style.get("template"),
        "rows": p.get("rows"),
        "columns": p.get("columns"),
        "reduce": reduce,
        "along": p.get("along"),
        "subplot_title": subplot_titles,
        "x_variable": p.get("x_variable"),
        "y_variable": p.get("y_variable"),
        "pair_on": p.get("pair_on"),
        "shared_scale": shared is True,
        "independent_scale": shared is False,
        "quiver_scale": scale,
        "quiver_step": step,
        "label": spec_input_labels(spec_data),
        "style": _style_from_spec(spec_data),
    }


def _style_from_spec(spec_data):
    if spec_data.get("layers"):
        return None
    kind = trace_at(spec_data).get("type")
    return kind if kind and kind not in {"layer", "layers"} else None


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
    base = overlay_spec(
        {"version": 1, "layout": {}, "style": {}, "geo": {}, "annotations": [], "shapes": []},
        spec_data or {},
    )
    ids = list(datasets.keys()) if isinstance(datasets, dict) else []
    if layers:
        base["traces"] = [{"type": "layer"}]
        base["layers"] = _layer_spec_entries(layers, datasets or {})
    elif style == "xy":
        trace = {"type": "xy"}
        if len(ids) >= 2:
            trace["x"], trace["y"] = ids[0], ids[1]
        elif ids:
            trace["input"] = ids[0]
        base["traces"] = [trace]
    else:
        base["traces"] = [{"type": style, "input": "a"}]
    resolved = overlay_flags(
        base,
        title=title,
        xlabel=xlabel,
        ylabel=ylabel,
        cbar_label=cbar_label,
        legend=legend,
        vmin=vmin,
        vmax=vmax,
        colormap=colormap,
        fontsize=fontsize,
        template=theme,
        figsize=figsize,
        extent=extent,
        cities=cities,
        bbox=bbox_nwse,
        mask_geojson=mask_geojson,
        draw_boxes=draw_boxes,
        pair_on=pair_on if style == "xy" else None,
        x_variable=x_variable if style == "xy" else None,
        y_variable=y_variable if style == "xy" else None,
        u_variable=u_variable,
        v_variable=v_variable,
    )
    if datasets:
        resolved["inputs"] = spec_inputs_from_datasets(datasets)
        if style == "xy" and resolved["inputs"]:
            if x_variable:
                resolved["inputs"][0]["variable"] = x_variable
            if y_variable and len(resolved["inputs"]) > 1:
                resolved["inputs"][1]["variable"] = y_variable
    if figsize is not None:
        resolved.setdefault("layout", {})["autosize"] = False
    return resolved


def _export_drawn_figure(fig, resolved, output, *, datasets, dump_spec, spec_data):
    from weather_skills_core.plot.export import write_plot_outputs

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
    spec_data,
    *,
    style,
    dump_spec_path,
    output,
    user_style,
):
    """Compile heatmap/timeseries/contour and write PNG + spec sidecar."""
    from weather_skills_core.plot.compile import compile_figure
    from weather_skills_core.plot.export import write_plot_outputs

    template = (spec_data.get("style") or {}).get("template") or user_style.get("template")
    input_path = _input_path_of(ds)
    defaults = spec_from_flags(
        trace_type=style,
        input_path=input_path,
        colormap=user_style.get("colormap"),
        fontsize=user_style.get("fontsize"),
        template=template,
    )
    merged = overlay_spec(defaults, spec_data or {})
    if not merged.get("traces"):
        merged["traces"] = [{"type": style, "input": "a"}]
    facet = merged.setdefault("layout", {}).setdefault("facet", {})
    if user_style.get("max_columns") and facet.get("rows") is None and facet.get("columns") is None:
        facet.setdefault("max_columns", user_style["max_columns"])

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
    help="X-axis Zarr for traces[].type xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--y",
    dest="y_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="Y-axis Zarr for traces[].type xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--layer",
    action="append",
    default=None,
    type=parse_layer,
    help=(
        "Map layer KIND:PATH or KIND:PATH::k=v. Repeat for overlays. "
        "Kinds: heatmap, scatter, quiver, outline, mask. "
        "Mutually exclusive with -i/--input. Layer options also belong in spec layers[]."
    ),
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
    "--patch",
    default=None,
    type=parse_json_object,
    help=(
        "Partial spec merged onto --spec (or onto defaults). "
        'Example: {"title": "Edited", "layout": {"colorbar": {"len": 0.45}}}.'
    ),
)
@weather_skill.argument(
    "--dump-spec",
    default=None,
    help=DUMP_SPEC_ARGUMENT_HELP,
)
def plot(
    ds,
    output,
    layer=None,
    x_ds=None,
    y_ds=None,
    spec=None,
    style_file=None,
    patch=None,
    dump_spec=None,
    **kwargs,
):
    """Render a heatmap, contour, timeseries, xy scatter, wind-rose, quiver, or layered map PNG from weather-skills Zarrs."""
    spec_data = _spec_data(spec)
    if patch:
        spec_data = overlay_spec(spec_data, patch)
    user_style = load_user_style(style_file)
    set_active_style(user_style)
    filled = _coerce_spec_params(spec_data, user_style)
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
    fontsize = filled["fontsize"]
    theme = filled["template"]
    rows = filled["rows"]
    columns = filled["columns"]
    reduce = filled["reduce"]
    along = filled["along"]
    subplot_title = filled["subplot_title"]
    x_variable = filled["x_variable"]
    y_variable = filled["y_variable"]
    pair_on = filled["pair_on"]
    shared_scale = filled["shared_scale"]
    independent_scale = filled["independent_scale"]
    quiver_scale = filled["quiver_scale"]
    quiver_step = filled["quiver_step"]
    label = filled["label"]
    style = filled["style"]
    spec_data = overlay_flags(
        spec_data,
        colormap=filled["colormap"],
        colormap_bounds=filled.get("colormap_bounds"),
        colormap_under=filled.get("colormap_under"),
        colormap_over=filled.get("colormap_over"),
        cbar_ticks=filled.get("cbar_ticks"),
        cbar_labels=filled.get("cbar_labels"),
    )
    colormap = spec_get(spec_data, "colormap")
    layers = list(layer or [])
    if not layers:
        layers = _layers_from_spec(spec_data, spec)
    if style is None and not layers:
        style = "heatmap"
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
            raise UsageError("--layer cannot be used with traces[].type xy")
        if x_ds is None and y_ds is None:
            if ds is None:
                raise UsageError(
                    "traces[].type xy needs --x and --y, or -i with traces[].x_variable "
                    "and traces[].y_variable in --spec"
                )
            if not x_variable or not y_variable:
                raise UsageError(
                    "with a single -i, traces[].type xy needs both traces[].x_variable "
                    "and traces[].y_variable in --spec"
                )
            x_ds = ds
            y_ds = ds
        elif x_ds is None or y_ds is None:
            raise UsageError("traces[].type xy needs both --x and --y")
    elif not layers and ds is None:
        raise UsageError("pass -i/--input, a --spec with inputs, or at least one --layer")
    if layers and style in ("timeseries", "xy", "windrose", "contour"):
        raise UsageError(f"--layer cannot be used with --style {style}")
    if layers and style == "quiver":
        raise UsageError(
            "with --layer, draw wind vectors as --layer quiver:PATH instead of --style quiver"
        )

    try:
        overrides = parse_index(index)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    bbox_nwse = bbox
    draw_boxes = parse_draw_boxes(draw_box)

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
            spec_data,
            style=style,
            dump_spec_path=dump_spec,
            output=output,
            user_style=user_style,
        )

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter

    apply_style(fontsize, template=theme or "weather_skills")
    apply_rc((spec_data.get("style") or {}).get("rc"))

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
            template=theme or "weather_skills",
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


patch_parser_for_spec_flags(plot.parser)


if __name__ == "__main__":
    plot()

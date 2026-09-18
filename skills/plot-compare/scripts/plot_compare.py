# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@plot-refactor",
#   "cf-xarray",
#   "cftime",
#   "matplotlib>=3.8",
#   "numpy",
#   "shapely>=2.1",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Side-by-side multi-panel PNG comparing two weather-skills standard dataset Zarrs."""

import sys

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.display_labels import dataset_display_label, resolve_input_labels
from weather_skills_core.plot.compile import (
    axis_kind,
    calendar_bin_width,
    format_calendar_panel,
    is_cftime_axis,
)
from weather_skills_core.plot.figure import (
    DEFAULT_FONTSIZE,
    axis_label,
    parse_figsize,
    parse_label_list,
    parse_number_list,
    resolve_axis_label,
)
from weather_skills_core.plot.spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    datasets_from_cli_or_spec,
    dump_spec_dest,
    overlay_flags,
    parse_plot_spec,
    resolve_flags,
    spec_get,
    spec_input_labels,
    spec_inputs_from_datasets,
)
from weather_skills_core.plot.style import (
    PRECIP_LONG_MIN_DAYS,
    aggregation_days,
    is_precip,
    is_precip_anomaly,
)
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    lat_slice,
    pick_time_dim,
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

_aggregation_days = aggregation_days
_axis_kind = axis_kind
_axis_label = axis_label
_format_single = format_calendar_panel
_is_cftime_axis = is_cftime_axis
_is_precip = is_precip
_is_precip_anomaly = is_precip_anomaly
_resolve_axis_label = resolve_axis_label


def _is_station(ds):
    return "station_id" in ds.dims


def _ax_bounds(ds, variable):
    import numpy as np

    if _is_station(ds):
        lons, lats = ds["longitude"].values, ds["latitude"].values
    else:
        lat_dim = cf_dim(ds[variable], "latitude")
        lon_dim = cf_dim(ds[variable], "longitude")
        lons, lats = ds[lon_dim].values, ds[lat_dim].values
    return (
        float(np.nanmin(lons)),
        float(np.nanmax(lons)),
        float(np.nanmin(lats)),
        float(np.nanmax(lats)),
    )


@weather_skill(
    name="plot-compare",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=False)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--variable-a",
    default=None,
    help="Variable for row A. Overrides --variable. Default: --variable or auto.",
)
@weather_skill.argument(
    "--variable-b",
    default=None,
    help="Variable for row B. Overrides --variable. Default: --variable or auto.",
)
@weather_skill.argument(
    "--colormap",
    default=None,
    help=(
        "matplotlib colormap name, or comma-separated colors. "
        "Precip default: discrete CHIRPS-GEFS classes (BoundaryNorm). "
        "Discrete custom classes: pass --colormap-bounds or a spec object."
    ),
)
@weather_skill.argument(
    "--colormap-bounds",
    default=None,
    type=parse_number_list,
    help="Comma-separated class stops; folds into style.colormap.bounds.",
)
@weather_skill.argument("--colormap-under", default=None, help="Color below the first class stop.")
@weather_skill.argument("--colormap-over", default=None, help="Color above the last class stop.")
@weather_skill.argument(
    "--cbar-ticks",
    default=None,
    type=parse_number_list,
    help="Comma-separated colorbar tick positions.",
)
@weather_skill.argument(
    "--cbar-labels",
    default=None,
    type=parse_label_list,
    help="Comma-separated colorbar tick labels. Requires --cbar-ticks.",
)
@weather_skill.argument(
    "--colormap-a",
    default=None,
    help="Colormap for row A in independent-scale mode (name or comma-separated colors).",
)
@weather_skill.argument(
    "--colormap-b",
    default=None,
    help="Colormap for row B in independent-scale mode (name or comma-separated colors).",
)
@weather_skill.argument(
    "--shared-scale",
    action="store_true",
    help="Force one shared color scale across both rows.",
)
@weather_skill.argument(
    "--independent-scale",
    action="store_true",
    help="Force per-row color scales.",
)
@weather_skill.argument("--panels", type=int, default=None)
@weather_skill.argument(
    "--time-dim", default=None, help="Override the time axis. Defaults to time, else step."
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--xlabel",
    default=None,
    help="Override the bottom x-axis label (default: Longitude).",
)
@weather_skill.argument(
    "--fontsize",
    type=int,
    default=DEFAULT_FONTSIZE,
    help="Base font size for panel titles, row labels, ticks, and colorbars (default 16).",
)
@weather_skill.argument(
    "--figsize",
    default=None,
    type=parse_figsize,
    help="Figure size W,H inches (e.g. 10,6 or 10x6). Default 22,10.",
)
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help="Row label for each --input, in order. Omit to infer from metadata.",
)
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; gridded cells outside become NaN.",
)
@weather_skill.argument(
    "--vmin",
    type=float,
    default=None,
    help="Colorbar lower limit. Unset = data min (or discrete precip classes).",
)
@weather_skill.argument(
    "--vmax",
    type=float,
    default=None,
    help="Colorbar upper limit. Unset = data max (or discrete precip classes).",
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
def plot_compare(
    ds,
    bbox,
    variable,
    variable_a,
    variable_b,
    colormap,
    colormap_a,
    colormap_b,
    shared_scale,
    independent_scale,
    title,
    xlabel,
    fontsize,
    figsize,
    panels,
    time_dim,
    label,
    mask_geojson,
    output,
    vmin=None,
    vmax=None,
    spec=None,
    dump_spec=None,
    colormap_bounds=None,
    colormap_under=None,
    colormap_over=None,
    cbar_ticks=None,
    cbar_labels=None,
    **kwargs,
):
    """Side-by-side multi-panel PNG comparing two weather-skills standard dataset Zarrs."""
    ds_a, ds_b = datasets_from_cli_or_spec(ds, spec, exactly=2)
    spec_data = spec.to_dict() if spec is not None else {}
    flags = resolve_flags(
        spec_data,
        title=title,
        xlabel=xlabel,
        colormap=colormap,
        colormap_a=colormap_a,
        colormap_b=colormap_b,
        colormap_bounds=colormap_bounds,
        colormap_under=colormap_under,
        colormap_over=colormap_over,
        cbar_ticks=cbar_ticks,
        cbar_labels=cbar_labels,
        figsize=figsize,
        panels=panels,
        bbox=bbox,
        mask_geojson=mask_geojson,
        vmin=vmin,
        vmax=vmax,
        variable=variable,
        variable_a=variable_a,
        variable_b=variable_b,
    )
    title, xlabel = flags["title"], flags["xlabel"]
    colormap, colormap_a, colormap_b = (
        flags["colormap"],
        flags["colormap_a"],
        flags["colormap_b"],
    )
    spec_data = overlay_flags(
        spec_data,
        colormap=colormap,
        colormap_a=colormap_a,
        colormap_b=colormap_b,
        colormap_bounds=flags.get("colormap_bounds"),
        colormap_under=flags.get("colormap_under"),
        colormap_over=flags.get("colormap_over"),
        cbar_ticks=flags.get("cbar_ticks"),
        cbar_labels=flags.get("cbar_labels"),
    )
    colormap = spec_get(spec_data, "colormap")
    colormap_a = spec_get(spec_data, "colormap_a")
    colormap_b = spec_get(spec_data, "colormap_b")
    bbox, mask_geojson = flags["bbox"], flags["mask_geojson"]
    vmin, vmax = flags["vmin"], flags["vmax"]
    variable, variable_a, variable_b = (
        flags["variable"],
        flags["variable_a"],
        flags["variable_b"],
    )
    figsize = tuple(flags["figsize"]) if flags["figsize"] else None
    panels = flags["panels"] or 3
    if not shared_scale and not independent_scale:
        shared = spec_get(spec_data, "shared_colorscale")
        if shared is True:
            shared_scale = True
        elif shared is False:
            independent_scale = True
    if not label:
        label = spec_input_labels(spec_data)
    if shared_scale and independent_scale:
        raise UsageError("--shared-scale and --independent-scale are mutually exclusive.")

    label_slots = resolve_input_labels(label, 2)
    label_a = label_slots[0] or dataset_display_label(ds_a, "A")
    label_b = label_slots[1] or dataset_display_label(ds_b, "B")

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np

    var_a = variable_a or variable or auto_variable(ds_a)
    var_b = variable_b or variable or auto_variable(ds_b)
    for side, var, ds in (("A", var_a, ds_a), ("B", var_b, ds_b)):
        if var is None or var not in ds:
            mapping_targets = {
                ds[d].attrs.get("grid_mapping")
                for d in ds.data_vars
                if ds[d].attrs.get("grid_mapping")
            }
            real_vars = [
                v
                for v in ds.data_vars
                if "grid_mapping_name" not in ds[v].attrs and v not in mapping_targets
            ]
            raise UsageError(
                f"variable '{var}' must exist in input {side}. {side} real data vars: {real_vars}"
            )

    ds_a = precip_for_display(to_standard_units(ds_a, variables=[var_a]), var_a)
    ds_b = precip_for_display(to_standard_units(ds_b, variables=[var_b]), var_b)

    try:
        td_a = pick_time_dim(ds_a, time_dim)
        td_b = pick_time_dim(ds_b, time_dim)
    except UsageError:
        raise UsageError(
            f"both inputs need a time/step dim. A: {list(ds_a.dims)}  B: {list(ds_b.dims)}"
        ) from None

    raw_a = ds_a[td_a].values
    raw_b = ds_b[td_b].values
    kind_a = _axis_kind(raw_a)
    kind_b = _axis_kind(raw_b)
    if kind_a is None or kind_b is None or kind_a != kind_b:
        raise DataError(
            "the two inputs have different time resolutions "
            f"('{td_a}' dtype={raw_a.dtype}, '{td_b}' dtype={raw_b.dtype}); "
            "aggregate both inputs to a common resolution first, e.g. with the "
            "aggregate-temporal skill."
        )

    a_is_cftime = _is_cftime_axis(raw_a)
    b_is_cftime = _is_cftime_axis(raw_b)
    if a_is_cftime != b_is_cftime:
        cf_td = td_a if a_is_cftime else td_b
        std_td = td_b if a_is_cftime else td_a
        raise DataError(
            "cannot compare a model-calendar (cftime) time axis "
            f"('{cf_td}') against a standard-calendar (datetime64) time axis "
            f"('{std_td}'); convert both to a common calendar first with the "
            "convert-calendar skill."
        )
    if a_is_cftime and b_is_cftime and raw_a.flat[0].calendar != raw_b.flat[0].calendar:
        raise DataError(
            "the two inputs use different model calendars "
            f"('{td_a}' calendar={raw_a.flat[0].calendar!r} vs "
            f"'{td_b}' calendar={raw_b.flat[0].calendar!r}); "
            "convert both to a common calendar first with the convert-calendar skill."
        )

    cftime_axes = a_is_cftime and b_is_cftime
    if cftime_axes:
        import cftime

        _epoch = "days since 1970-01-01"
        enc_a = np.asarray(
            cftime.date2num(raw_a, units=_epoch, calendar=raw_a.flat[0].calendar), dtype="float64"
        )
        enc_b = np.asarray(
            cftime.date2num(raw_b, units=_epoch, calendar=raw_b.flat[0].calendar), dtype="float64"
        )
        tol_enc = 1.0 / 86400.0
    else:
        ns_dtype = "datetime64[ns]" if kind_a == "datetime" else "timedelta64[ns]"
        enc_a = raw_a.astype(ns_dtype).astype("int64")
        enc_b = raw_b.astype(ns_dtype).astype("int64")
        tol_enc = 1_000_000_000

    def _median_spacing(enc_values, ds, dim):
        bound_name = ds[dim].attrs.get("bounds") if dim in ds else None
        if isinstance(bound_name, str) and bound_name in ds:
            pairs = np.asarray(ds[bound_name].values)
            if pairs.ndim == 2 and pairs.shape[1] == 2:
                try:
                    widths = np.abs(
                        pairs[:, 1].astype("timedelta64[ns]").astype("int64")
                        - pairs[:, 0].astype("timedelta64[ns]").astype("int64")
                    )
                except (TypeError, ValueError):
                    try:
                        widths = np.abs(
                            pairs[:, 1].astype("datetime64[ns]").astype("int64")
                            - pairs[:, 0].astype("datetime64[ns]").astype("int64")
                        )
                    except (TypeError, ValueError):
                        widths = None
                if widths is not None and widths.size:
                    return float(np.median(widths))
        if enc_values.size < 2:
            return None
        return float(np.median(np.abs(np.diff(enc_values))))

    width_a = _median_spacing(enc_a, ds_a, td_a)
    width_b = _median_spacing(enc_b, ds_b, td_b)
    if width_a is not None and width_b is not None:
        rel = abs(width_a - width_b) / max(width_a, width_b, 1.0)
        if rel > 1e-3:
            if cftime_axes:
                wa_str, wb_str = f"{width_a:.4g} days", f"{width_b:.4g} days"
            else:
                wa_str, wb_str = f"{width_a:.0f} ns", f"{width_b:.0f} ns"
            raise DataError(
                "the two inputs have different time resolutions "
                f"(median bin width '{td_a}'≈{wa_str} vs '{td_b}'≈{wb_str}); "
                "aggregate both inputs to a common resolution first, e.g. with the "
                "aggregate-temporal skill."
            )

    order_a = np.argsort(enc_a, kind="stable")
    sorted_enc_a = enc_a[order_a]
    sorted_enc_b = np.sort(enc_b)
    common_enc, common_src = [], []
    for pos, va in zip(order_a, sorted_enc_a, strict=True):
        idx = np.searchsorted(sorted_enc_b, va)
        nearest = None
        for cand in (idx - 1, idx):
            if 0 <= cand < sorted_enc_b.size:
                d = abs(float(sorted_enc_b[cand]) - float(va))
                if nearest is None or d < nearest:
                    nearest = d
        if nearest is not None and nearest <= tol_enc:
            common_enc.append(va)
            common_src.append(int(pos))

    if not common_enc:
        raise DataError(f"no overlapping time bins between the two inputs on '{td_a}'/'{td_b}'.")

    n = min(panels, len(common_enc))
    src_last = common_src[-n:]
    if cftime_axes:
        import datetime as _dt

        common_labels = np.asarray(raw_a, dtype=object)[src_last]
        common_tol = _dt.timedelta(seconds=1)
    else:
        common_labels = np.asarray(common_enc[-n:], dtype="int64").astype(ns_dtype)
        common_tol = np.timedelta64(1, "s")

    da_a = ds_a[var_a]
    da_b = ds_b[var_b]
    units_a = variable_units(da_a)
    units_b = variable_units(da_b)
    units_match = (
        isinstance(units_a, str) and isinstance(units_b, str) and units_equal(units_a, units_b)
    )
    if shared_scale:
        use_shared_scale = True
    elif independent_scale:
        use_shared_scale = False
    else:
        use_shared_scale = var_a == var_b and units_match

    if (
        use_shared_scale
        and not units_match
        and isinstance(units_a, str)
        and isinstance(units_b, str)
    ):
        print(
            f"Warning: the two rows have differing units "
            f"({label_a} {var_a!r} units={units_a!r}, "
            f"{label_b} {var_b!r} units={units_b!r}). "
            f"The two rows are drawn on one shared color scale, so values in "
            f"different units are not directly comparable in this figure.",
            file=sys.stderr,
        )

    a_lat = cf_dim(da_a, "latitude")
    a_lon = cf_dim(da_a, "longitude")
    b_lat = cf_dim(da_b, "latitude")
    b_lon = cf_dim(da_b, "longitude")
    spatial_dims = {a_lat, a_lon, b_lat, b_lon} - {None}

    def _flatten(da, tdim):
        for d in list(da.dims):
            if d == tdim or d == "station_id" or d in spatial_dims:
                continue
            da = da.mean(d) if d == "number" else da.isel({d: 0}, drop=True)
        return da

    da_a = _flatten(da_a, td_a)
    da_b = _flatten(da_b, td_b)

    region_bbox = bbox
    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None

    if region_bbox is not None or region_polygon is not None:
        r_n, r_w, r_s, r_e = region_bbox if region_bbox is not None else (None, None, None, None)
        for side, ds_label in (("a", label_a), ("b", label_b)):
            ds = ds_a if side == "a" else ds_b
            da = da_a if side == "a" else da_b
            if _is_station(ds):
                if region_bbox is not None:
                    lons, lats = ds["longitude"].values, ds["latitude"].values
                    lon_keep = (
                        (lons >= r_w) | (lons <= r_e)
                        if r_w > r_e
                        else (lons >= r_w) & (lons <= r_e)
                    )
                    keep = lon_keep & (lats >= r_s) & (lats <= r_n)
                    keep_ids = ds["station_id"].values[keep]
                    if len(keep_ids) == 0:
                        print(
                            f"Warning: 0 stations inside --bbox {r_n}/{r_w}/{r_s}/{r_e} "
                            f"on input '{ds_label}'; scatter will render empty.",
                            file=sys.stderr,
                        )
                    ds = ds.sel(station_id=keep_ids)
                    da = da.sel(station_id=keep_ids)
                    if r_w > r_e:
                        shifted_lon = ((ds["longitude"].values - r_w) % 360.0) + r_w
                        ds = ds.assign_coords(longitude=("station_id", shifted_lon))
            else:
                lat_dim = cf_dim(da, "latitude")
                lon_dim = cf_dim(da, "longitude")
                if lat_dim is not None and lon_dim is not None:
                    ds = ensure_normalized_longitude(ds, lon_dim)
                    da = ensure_normalized_longitude(da, lon_dim)
                    if region_bbox is not None:
                        lat_sl = lat_slice(da[lat_dim].values, r_n, r_s)
                        ds = ds.sel({lat_dim: lat_sl})
                        da = da.sel({lat_dim: lat_sl})
                        if r_w > r_e:
                            ds = ds.where((ds[lon_dim] >= r_w) | (ds[lon_dim] <= r_e), drop=True)
                            da = da.where((da[lon_dim] >= r_w) | (da[lon_dim] <= r_e), drop=True)
                        else:
                            ds = ds.sel({lon_dim: slice(r_w, r_e)})
                            da = da.sel({lon_dim: slice(r_w, r_e)})
                    if region_polygon is not None:
                        import shapely
                        import xarray as _xr

                        lon_grid, lat_grid = np.meshgrid(da[lon_dim].values, da[lat_dim].values)
                        mask = shapely.contains_xy(region_polygon, lon_grid, lat_grid)
                        if not bool(mask.any()):
                            print(
                                f"Warning: --mask-geojson polygon does not intersect "
                                f"input '{ds_label}'; its panel will be entirely empty.",
                                file=sys.stderr,
                            )
                        da = da.where(_xr.DataArray(mask, dims=(lat_dim, lon_dim)))
                    if region_bbox is not None and r_w > r_e:
                        ds = ds.assign_coords(
                            {lon_dim: ((ds[lon_dim] - r_w) % 360.0) + r_w}
                        ).sortby(lon_dim)
                        da = da.assign_coords(
                            {lon_dim: ((da[lon_dim] - r_w) % 360.0) + r_w}
                        ).sortby(lon_dim)
                elif region_bbox is not None:
                    print(
                        f"Warning: input '{ds_label}' has no CF lat/lon "
                        f"dims; --bbox {r_n}/{r_w}/{r_s}/{r_e} slice not applied.",
                        file=sys.stderr,
                    )
                elif region_polygon is not None:
                    print(
                        f"Warning: input '{ds_label}' has no CF lat/lon "
                        f"dims; --mask-geojson polygon not applied.",
                        file=sys.stderr,
                    )
            if side == "a":
                ds_a, da_a = ds, da
            else:
                ds_b, da_b = ds, da

    a_station = _is_station(ds_a)
    b_station = _is_station(ds_b)

    from weather_skills_core.plot.export import write_plot_outputs
    from weather_skills_core.plot.recipes import (
        compile_heatmap_grid,
        heatmap_cell,
        scale_from_da,
        scatter_cell,
    )

    user_vlim = vmin is not None or vmax is not None

    def _finite_limits(*das):
        data_min = float(np.nanmin([float(d.min().values) for d in das]))
        data_max = float(np.nanmax([float(d.max().values) for d in das]))
        if not np.isfinite(data_min) or not np.isfinite(data_max):
            return 0.0, 1.0
        return data_min, data_max

    def _apply_vlim(lo, hi, *, stretch_symmetric):
        if lo > hi:
            raise UsageError(f"--vmin/--vmax: lower limit {lo} is greater than upper limit {hi}")
        if lo == hi:
            pad = abs(lo) * 0.05 if lo != 0 else 1.0
            lo, hi = lo - pad, hi + pad
        elif stretch_symmetric and hi > 0 and lo < 0:
            m = max(abs(hi), abs(lo))
            lo, hi = -m, m
        return lo, hi

    def _indep_scale(da, cmap_name, label):
        if cmap_name or not _is_precip(da) or user_vlim:
            dmin, dmax = _finite_limits(da)
            lo = dmin if vmin is None else float(vmin)
            hi = dmax if vmax is None else float(vmax)
            lo, hi = _apply_vlim(lo, hi, stretch_symmetric=not user_vlim)
            return scale_from_da(da, cmap_name, stretch=True, label=label, vmin=lo, vmax=hi)
        return scale_from_da(da, cmap_name, stretch=False, label=label)

    def _cbar_label(da, label, var):
        return f"{label} {variable_label_for_display(da, fallback=var)}"

    label_scale_a = _cbar_label(da_a, label_a, var_a)
    label_scale_b = _cbar_label(da_b, label_b, var_b)
    if use_shared_scale:
        cmap_name = colormap
        if colormap is None and _is_precip(da_a) and _is_precip(da_b) and not user_vlim:
            if _is_precip_anomaly(da_a) or _is_precip_anomaly(da_b):
                cmap_name = "chirps_anom"
            else:
                days_a = _aggregation_days(da_a)
                days_b = _aggregation_days(da_b)
                both_short = (
                    days_a is not None
                    and days_a < PRECIP_LONG_MIN_DAYS
                    and days_b is not None
                    and days_b < PRECIP_LONG_MIN_DAYS
                )
                cmap_name = "chirps_short" if both_short else "chirps_total"
            scale_shared = scale_from_da(da_a, cmap_name, stretch=False)
        else:
            dmin, dmax = _finite_limits(da_a, da_b)
            lo = dmin if vmin is None else float(vmin)
            hi = dmax if vmax is None else float(vmax)
            lo, hi = _apply_vlim(lo, hi, stretch_symmetric=not user_vlim)
            scale_shared = scale_from_da(da_a, colormap, stretch=True, vmin=lo, vmax=hi)
        scale_a = {**scale_shared, "label": label_scale_a}
        scale_b = {**scale_shared, "label": label_scale_b}
    else:
        scale_a = _indep_scale(da_a, colormap_a or colormap, label_scale_a)
        scale_b = _indep_scale(da_b, colormap_b or colormap, label_scale_b)

    side_a = (ds_a, da_a, td_a, label_a, var_a, scale_a)
    side_b = (ds_b, da_b, td_b, label_b, var_b, scale_b)
    top, bottom = (side_b, side_a) if b_station and not a_station else (side_a, side_b)

    if a_station and not b_station:
        gridded_ds, gridded_var = ds_b, var_b
    elif b_station and not a_station:
        gridded_ds, gridded_var = ds_a, var_a
    else:
        gridded_ds, gridded_var = ds_a, var_a
    g_xmin, g_xmax, g_ymin, g_ymax = _ax_bounds(gridded_ds, gridded_var)
    wrapped_bbox = region_bbox is not None and region_bbox[1] > region_bbox[3]
    if region_bbox is not None:
        r_n, r_w, r_s, r_e = region_bbox
        g_xmin, g_ymin, g_ymax = r_w, r_s, r_n
        g_xmax = r_e + 360.0 if wrapped_bbox else r_e

    def _row_cells(row, scale_id):
        ds, da, td, _label, _var, _scale = row
        row_sel = da.sel({td: common_labels}, method="nearest", tolerance=common_tol)
        bin_width = calendar_bin_width(da, da[td].values)
        titles = [_format_single(row_sel[td].values[col], bin_width=bin_width) for col in range(n)]
        cells = []
        is_station = _is_station(ds)
        for col in range(n):
            sel = row_sel.isel({td: col})
            if is_station:
                cells.append(
                    scatter_cell(
                        ds["longitude"].values,
                        ds["latitude"].values,
                        sel.values,
                        scale=scale_id,
                    )
                )
            else:
                lat_dim = cf_dim(sel, "latitude")
                lon_dim = cf_dim(sel, "longitude")
                cells.append(heatmap_cell(sel, lat_dim, lon_dim, scale=scale_id))
        return cells, titles

    top_cells, col_titles = _row_cells(top, "top")
    bottom_cells, _ = _row_cells(bottom, "bottom")
    fig = compile_heatmap_grid(
        [top_cells, bottom_cells],
        extent=[g_xmin, g_xmax, g_ymin, g_ymax],
        title=title,
        col_titles=col_titles,
        row_titles=[_axis_label(top[3]), _axis_label(bottom[3])],
        fontsize=fontsize,
        figsize=figsize,
        scales={"top": top[5], "bottom": bottom[5]},
        xlabel=_resolve_axis_label(xlabel, "Longitude"),
        spec=spec_data,
    )
    datasets = {"a": ds_a, "b": ds_b}
    inputs = spec_inputs_from_datasets(datasets)
    if var_a:
        inputs[0]["variable"] = var_a
    if var_b:
        inputs[1]["variable"] = var_b
    if label_slots[0]:
        inputs[0]["label"] = label_slots[0]
    if label_slots[1]:
        inputs[1]["label"] = label_slots[1]
    geo_out = {}
    if bbox is not None:
        geo_out["bbox"] = list(bbox) if not isinstance(bbox, str) else bbox
    if mask_geojson:
        geo_out["mask_geojson"] = str(mask_geojson)
    resolved = {
        "version": 1,
        "skill": "plot-compare",
        "inputs": inputs,
        "layout": {
            "facet": {"rows": 2, "columns": n},
            "shared_colorscale": use_shared_scale,
            "figsize": list(figsize) if figsize else None,
        },
        "traces": [{"type": "heatmap_grid"}],
        "style": {
            "template": "weather_skills",
            "fontsize": fontsize,
            "colormap": colormap or top[5].get("name"),
        },
        "title": title,
        "xlabel": xlabel,
        "geo": geo_out,
    }
    for side, cmap in ((0, colormap_a), (1, colormap_b)):
        if cmap and side < len(resolved["inputs"]):
            resolved["inputs"][side]["colormap"] = cmap
    if vmin is not None:
        resolved["vmin"] = vmin
    if vmax is not None:
        resolved["vmax"] = vmax
    return write_plot_outputs(
        fig,
        resolved,
        output,
        datasets=datasets,
        dump_spec_path=dump_spec_dest(dump_spec),
        spec=spec_data,
    )


if __name__ == "__main__":
    plot_compare()

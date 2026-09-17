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
"""Lead-week verification as a grid of maps: obs, forecast, and verify metric."""

from __future__ import annotations

import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.display_labels import (
    combine_display_labels,
    dataset_display_label,
    resolve_input_labels,
)
from weather_skills_core.figure import (
    DEFAULT_FONTSIZE,
    format_plot_date_range,
    parse_figsize,
)
from weather_skills_core.plot_compile import extent_from_da, slice_bbox_mask
from weather_skills_core.plot_spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    dump_spec_dest,
    named_datasets_from_spec,
    parse_plot_spec,
    spec_inputs_from_datasets,
    spec_role_datasets,
)
from weather_skills_core.plot_style import aggregation_days
from weather_skills_core.standard_utils import polygon_from_geojson
from weather_skills_core.units import (
    format_units_for_display,
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

_aggregation_days = aggregation_days
_extent_from_da = extent_from_da
_slice_bbox_mask = slice_bbox_mask

_VERIFY_VARS = {
    "hits": "event_hit",
    "bias": "bias",
    "mae": "mae",
}
_ROW_FALLBACKS = ("Observation", "Forecast", "Verification")
_METRIC_ROW_LABELS = {"hits": "Hits", "bias": "Bias", "mae": "MAE"}


def _metric_from_verify(ds, role: str) -> str:
    metric = ds.attrs.get("verify_metric")
    if metric not in _VERIFY_VARS:
        raise UsageError(
            f"{role} is missing a supported verify_metric attr "
            f"({list(_VERIFY_VARS)}); run the verify skill first."
        )
    return metric


def _verify_field(ds, metric: str, role: str):
    name = _VERIFY_VARS[metric]
    if name not in ds:
        raise UsageError(f"{role} missing verification variable {name!r}.")
    return ds[name]


def _row_labels(obs, forecasts, metric="hits", labels=None):
    """Y-axis product names: one short label per row, not per --forecast file."""
    n_fc = len(forecasts)
    slots = (
        resolve_input_labels(labels, 1 + n_fc, input_flag="--obs and --forecast")
        if labels
        else [None] * (1 + n_fc)
    )
    obs_label = slots[0] or dataset_display_label(obs, _ROW_FALLBACKS[0])
    fc_labels = [
        slot or dataset_display_label(ds, _ROW_FALLBACKS[1])
        for slot, ds in zip(slots[1:], forecasts, strict=True)
    ]
    forecast_label = combine_display_labels(fc_labels)
    verify_label = _METRIC_ROW_LABELS.get(metric, _ROW_FALLBACKS[2])
    return (obs_label, forecast_label, verify_label)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _dim(ds, *names: str) -> str | None:
    return next((n for n in names if n in ds.dims), None)


def _median_spacing(coord) -> float | None:
    import numpy as np

    vals = coord.values
    if getattr(vals, "size", 0) < 2:
        return None
    return float(np.median(np.abs(np.diff(np.asarray(vals, dtype=float)))))


def _require_same_grid(left, right, left_role, right_role) -> None:
    """Refuse when lat/lon spacing differs (coarsen obs onto the forecast, not the reverse)."""
    import numpy as np

    lat_a = _dim(left, "latitude", "lat")
    lon_a = _dim(left, "longitude", "lon")
    lat_b = _dim(right, "latitude", "lat")
    lon_b = _dim(right, "longitude", "lon")
    if not all((lat_a, lon_a, lat_b, lon_b)):
        return
    pairs = (
        (_median_spacing(left[lat_a]), _median_spacing(right[lat_b]), "latitude"),
        (_median_spacing(left[lon_a]), _median_spacing(right[lon_b]), "longitude"),
    )
    mismatched = [
        axis
        for d_a, d_b, axis in pairs
        if d_a is not None and d_b is not None and not np.isclose(d_a, d_b, rtol=0.01, atol=1e-6)
    ]
    if mismatched:
        raise UsageError(
            f"{right_role} grid spacing does not match {left_role} on "
            f"{' and '.join(mismatched)}; coarsen --obs onto the forecast "
            "with --reference-grid <forecast.zarr> (match obs to the forecast "
            "resolution, not the reverse)."
        )


def _coord_as_date(val, *, units=None, calendar=None):
    """Best-effort date from a numpy/cftime/datetime/CF-encoded time value."""
    from datetime import date, datetime

    import numpy as np

    if val is None:
        return None
    try:
        if isinstance(val, float) and np.isnan(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, np.datetime64):
        if np.isnat(val):
            return None
        iso = str(val.astype("datetime64[D]"))
        try:
            return date.fromisoformat(iso[:10])
        except ValueError:
            return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if hasattr(val, "year") and hasattr(val, "month") and hasattr(val, "day"):
        try:
            return date(int(val.year), int(val.month), int(val.day))
        except (TypeError, ValueError):
            return None
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", "replace")
    if isinstance(val, str):
        try:
            return date.fromisoformat(val[:10])
        except ValueError:
            pass
    if units and isinstance(val, (int, np.integer, float, np.floating)):
        try:
            import cftime

            dt = cftime.num2date(val, units=units, calendar=calendar or "standard")
            return date(int(dt.year), int(dt.month), int(dt.day))
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _time_coord(da, ds=None):
    names = ("time", "valid_time")
    for obj in (da, ds):
        if obj is None:
            continue
        for name in names:
            if name in getattr(obj, "coords", {}) or name in getattr(obj, "dims", ()):
                return obj[name]
    return None


def _verifying_week_title(da, ds=None):
    """Obs-week dates, e.g. ``30 Aug–5 Sept '26``, or None if time is missing."""
    from datetime import timedelta

    import numpy as np

    coord = _time_coord(da, ds)
    start = None
    if coord is not None:
        vals = np.asarray(coord.values).reshape(-1)
        if vals.size:
            start = _coord_as_date(
                vals[0],
                units=coord.attrs.get("units") if hasattr(coord, "attrs") else None,
                calendar=coord.attrs.get("calendar") if hasattr(coord, "attrs") else None,
            )
    if start is None:
        return None
    days = _aggregation_days(da)
    if days is None and ds is not None:
        days = _aggregation_days(ds)
    span = int(round(days)) if days and days >= 2 else 7
    end = start + timedelta(days=span - 1)

    return format_plot_date_range(start, end)


def _lead_week_number(label):
    """Week index from a lead title, or None if the label has no week number."""
    import re

    text = str(label)
    match = re.search(r"week[-\s]*(\d+)", text, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)[-\s]*week", text, re.I)
    if match:
        return int(match.group(1))
    return None


def _order_week1_first(leads, forecasts, verify_sets, labels=None):
    """Sort paired lead columns week-1 … week-N when every label names a week."""
    keys = [_lead_week_number(label) for label in leads]
    if any(key is None for key in keys):
        return leads, forecasts, verify_sets, labels
    order = sorted(range(len(leads)), key=lambda i: keys[i])

    def _reorder(items):
        return [items[i] for i in order]

    leads = _reorder(leads)
    forecasts = _reorder(forecasts)
    verify_sets = _reorder(verify_sets)
    if labels and len(labels) == 1 + len(order):
        labels = [labels[0], *_reorder(labels[1:])]
    return leads, forecasts, verify_sets, labels


def _variable_label(da):
    name = da.attrs.get("long_name") or da.attrs.get("GRIB_name") or da.name or "value"
    units = format_units_for_display(variable_units(da) or da.attrs.get("units"))
    if units:
        return f"{name} [{units}]"
    return str(name)


def _lat_lon(da, role):
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if lat_dim is None or lon_dim is None or lat_dim not in da.dims or lon_dim not in da.dims:
        raise UsageError(f"{role} needs lat/lon as dimensions; got {list(da.dims)}")
    return lat_dim, lon_dim


def _squeeze_map(da, role):
    """Reduce to a single lat/lon field (the verifying week)."""
    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)
    if "step" in da.dims and "time" not in da.dims:
        raise UsageError(
            f"{role} still has a step axis; run step-to-time and select the "
            "verifying week before plot-verify so valid times align with --obs."
        )
    lat_dim, lon_dim = _lat_lon(da, role)
    extras = [d for d in da.dims if d not in (lat_dim, lon_dim)]
    for dim in extras:
        if da.sizes[dim] != 1:
            raise UsageError(
                f"{role} has {dim} size {da.sizes[dim]}; select the verifying "
                "week (one time) before plot-verify."
            )
        da = da.squeeze(dim, drop=True)
    return da


def _pick_variable(ds, variable, role):
    name = variable or auto_variable(ds)
    if not name or name not in ds:
        raise UsageError(f"variable {name!r} missing from {role}. Available: {list(ds.data_vars)}")
    return name


def _prepare(ds, variable):
    return precip_for_display(to_standard_units(ds, variables=[variable]), variable)


@weather_skill(
    name="plot-verify",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--obs", type=Dataset("spatial"), required=False)
@weather_skill.argument(
    "--forecast",
    type=Dataset("spatial"),
    action="append",
    required=False,
)
@weather_skill.argument(
    "--verify",
    type=Dataset("any"),
    action="append",
    required=False,
    help="Verify Zarr from the verify skill, once per --forecast (same order).",
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--lead",
    action="append",
    default=None,
    help=(
        "Column label, once per --forecast. Default: 1-week lead … N-week lead "
        "(week-1 first). Labels that name a week are sorted week-1 → week-N."
    ),
)
@weather_skill.argument(
    "--colormap",
    default=None,
    help=(
        "matplotlib colormap name, or comma-separated colors, for obs/forecast rows. "
        "Default: discrete CHIRPS-GEFS precip classes for precip, else viridis."
    ),
)
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help=(
        "Display label for row titles. Pass once for --obs, then once per --forecast "
        "(same order). Omit to infer from provenance or weather_skills_source."
    ),
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--fontsize",
    type=int,
    default=DEFAULT_FONTSIZE,
    help="Base font size for column/row labels, ticks, and colorbars (default 16).",
)
@weather_skill.argument(
    "--figsize",
    default=None,
    type=parse_figsize,
    help="Figure size W,H inches (e.g. 10,6 or 10x6). Default from map grid.",
)
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; gridded cells outside become NaN.",
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
def plot_verify(
    obs,
    forecast,
    verify,
    bbox,
    variable,
    lead,
    colormap,
    label,
    title,
    fontsize,
    figsize,
    mask_geojson,
    output,
    spec=None,
    dump_spec=None,
    **kwargs,
):
    """Lead-week verification grid from obs, forecast, and pre-computed verify Zarrs."""
    spec_data = spec.to_dict() if spec is not None else {}
    named_spec = named_datasets_from_spec(spec) if spec is not None else {}
    layout = spec_data.get("layout") or {}
    style_block = spec_data.get("style") or {}
    geo = spec_data.get("geo") or {}
    first_input = (spec_data.get("inputs") or [{}])[0]
    if not isinstance(first_input, dict):
        first_input = {}
    if obs is None:
        obs = named_spec.get("obs")
    forecasts = _as_list(forecast)
    if not forecasts:
        forecasts = spec_role_datasets(named_spec, "forecast")
    verify_sets = _as_list(verify)
    if not verify_sets:
        verify_sets = spec_role_datasets(named_spec, "verify")
    title = title if title is not None else spec_data.get("title")
    colormap = colormap or style_block.get("colormap")
    if figsize is None and layout.get("figsize"):
        figsize = tuple(layout["figsize"])
    if bbox is None:
        bbox = geo.get("bbox")
    if mask_geojson is None:
        mask_geojson = geo.get("mask_geojson")
    variable = variable or first_input.get("variable")
    if not lead:
        lead = layout.get("leads")
    if not label:
        label = [
            item.get("label")
            for item in spec_data.get("inputs") or []
            if isinstance(item, dict) and not str(item.get("id", "")).startswith("verify")
        ]
        if not any(label):
            label = None
    if obs is None:
        raise UsageError("pass --obs, or --spec with an obs input.")
    if not forecasts:
        raise UsageError("expected at least one --forecast, or --spec with forecast inputs.")
    if len(verify_sets) != len(forecasts):
        raise UsageError(
            f"--verify was passed {len(verify_sets)} time(s) but --forecast was passed "
            f"{len(forecasts)} time(s); pass one --verify per --forecast."
        )
    leads = _as_list(lead)
    if leads and len(leads) != len(forecasts):
        raise UsageError(
            f"--lead was passed {len(leads)} time(s) but --forecast was passed "
            f"{len(forecasts)} time(s); pass one --lead per --forecast."
        )
    if not leads:
        leads = [f"{i}-week lead" for i in range(1, len(forecasts) + 1)]
    labels = _as_list(label) or None
    leads, forecasts, verify_sets, labels = _order_week1_first(
        leads, forecasts, verify_sets, labels
    )

    metrics = [_metric_from_verify(ds, f"--verify {i + 1}") for i, ds in enumerate(verify_sets)]
    if len(set(metrics)) != 1:
        raise UsageError(f"all --verify inputs must share the same verify_metric; got {metrics}.")
    metric = metrics[0]
    row_labels = _row_labels(obs, forecasts, metric, labels=labels)

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np

    obs_name = _pick_variable(obs, variable, "--obs")
    fc_names = [
        _pick_variable(fc, variable, f"--forecast {i + 1}") for i, fc in enumerate(forecasts)
    ]
    obs_ds = _prepare(obs, obs_name)
    fc_datasets = [_prepare(fc, name) for fc, name in zip(forecasts, fc_names, strict=True)]

    u_obs = variable_units(obs_ds[obs_name])
    for i, (fc_ds, fc_name) in enumerate(zip(fc_datasets, fc_names, strict=True)):
        u_fc = variable_units(fc_ds[fc_name])
        if (
            isinstance(u_fc, str)
            and u_fc.strip()
            and isinstance(u_obs, str)
            and u_obs.strip()
            and not units_equal(u_fc, u_obs)
        ):
            print(
                f"Warning: --forecast {i + 1} {fc_name!r} units={u_fc.strip()!r} and "
                f"--obs {obs_name!r} units={u_obs.strip()!r} differ.",
                file=sys.stderr,
            )

    _require_same_grid(fc_datasets[0], obs_ds, "--forecast 1", "--obs")
    for i, fc_ds in enumerate(fc_datasets[1:], start=2):
        _require_same_grid(fc_datasets[0], fc_ds, "--forecast 1", f"--forecast {i}")

    polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    obs_da = _squeeze_map(obs_ds[obs_name], "--obs")
    obs_lat, obs_lon = _lat_lon(obs_da, "--obs")
    obs_da = _slice_bbox_mask(obs_da, obs_lat, obs_lon, bbox, polygon, "--obs")
    week_dates = _verifying_week_title(obs_ds[obs_name], obs_ds)

    columns = []
    for i, (fc_ds, fc_name, verify_ds, label) in enumerate(
        zip(fc_datasets, fc_names, verify_sets, leads, strict=True), start=1
    ):
        role = f"--forecast {i} ({label})"
        fc_da = _squeeze_map(fc_ds[fc_name], role)
        lat_dim, lon_dim = _lat_lon(fc_da, role)
        fc_da = _slice_bbox_mask(fc_da, lat_dim, lon_dim, bbox, polygon, role)
        verify_da = _squeeze_map(
            _verify_field(verify_ds, metric, f"--verify {i}"),
            f"--verify {i}",
        )
        verify_da = _slice_bbox_mask(verify_da, lat_dim, lon_dim, bbox, polygon, f"--verify {i}")
        summary = verify_ds.attrs.get("verify_score_summary")
        if isinstance(summary, str) and summary.strip():
            print(f"{label}  {summary.strip()}")
        columns.append((label, fc_da, verify_da, lat_dim, lon_dim))

    extent = _extent_from_da(obs_da, obs_lat, obs_lon, bbox)
    from weather_skills_core.plot_export import write_plot_outputs
    from weather_skills_core.plot_recipes import (
        blank_cell,
        compile_heatmap_grid,
        error_scale,
        heatmap_cell,
        hits_scale,
        scale_from_da,
    )

    field_scale = scale_from_da(obs_da, colormap, stretch=False, label=_variable_label(obs_da))
    if field_scale.get("bounds") is None:
        present = [float(obs_da.min(skipna=True).values), float(obs_da.max(skipna=True).values)]
        for _label, fc_da, *_rest in columns:
            present.append(float(fc_da.min(skipna=True).values))
            present.append(float(fc_da.max(skipna=True).values))
        vmin = float(np.nanmin(present))
        vmax = float(np.nanmax(present))
        if vmax > 0 and vmin < 0:
            m = max(abs(vmax), abs(vmin))
            vmin, vmax = -m, m
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
            vmin, vmax = 0.0, 1.0
        field_scale = scale_from_da(
            obs_da, colormap, stretch=True, label=_variable_label(obs_da), vmin=vmin, vmax=vmax
        )
    if metric == "hits":
        verify_scale = hits_scale()
    else:
        import xarray as xr

        stacked = xr.concat([col[2] for col in columns], dim="panel")
        units = format_units_for_display(u_obs)
        metric_label = _METRIC_ROW_LABELS[metric]
        caption = f"{metric_label} [{units}]" if units else metric_label
        verify_scale = error_scale(stacked, metric, label=caption)

    fig_title = title
    if week_dates and not (title and week_dates in title):
        fig_title = f"{title} · {week_dates}" if title else week_dates

    n_leads = len(columns)
    top_row = [heatmap_cell(obs_da, obs_lat, obs_lon, scale="field")]
    bottom_row = [blank_cell("")]
    col_titles = [row_labels[0]]
    for col_label, fc_da, verify_da, lat_dim, lon_dim in columns:
        top_row.append(heatmap_cell(fc_da, lat_dim, lon_dim, scale="field"))
        bottom_row.append(heatmap_cell(verify_da, lat_dim, lon_dim, scale="verify"))
        col_titles.append(col_label)

    fig = compile_heatmap_grid(
        [top_row, bottom_row],
        extent=extent,
        title=fig_title,
        col_titles=col_titles,
        row_titles=[row_labels[0], row_labels[2]],
        fontsize=fontsize,
        figsize=figsize,
        scales={"field": field_scale, "verify": verify_scale},
    )
    named = {
        "obs": obs,
        **{f"forecast{i}": fc for i, fc in enumerate(forecasts, start=1)},
        **{f"verify{i}": ds for i, ds in enumerate(verify_sets, start=1)},
    }
    inputs = spec_inputs_from_datasets(named)
    for item in inputs:
        key = str(item["id"])
        if key.startswith("forecast"):
            item["role"] = "forecast"
        elif key.startswith("verify"):
            item["role"] = "verify"
        else:
            item["role"] = "obs"
        if variable or obs_name:
            item["variable"] = variable or obs_name
    if labels:
        obs_and_fc = [item for item in inputs if item["role"] != "verify"]
        for i, item in enumerate(obs_and_fc):
            if i < len(labels) and labels[i]:
                item["label"] = labels[i]
    geo_out = {}
    if bbox is not None:
        geo_out["bbox"] = list(bbox) if not isinstance(bbox, str) else bbox
    if mask_geojson:
        geo_out["mask_geojson"] = str(mask_geojson)
    resolved = {
        "version": 1,
        "skill": "plot-verify",
        "inputs": inputs,
        "layout": {
            "rows": 2,
            "columns": 1 + n_leads,
            "metric": metric,
            "leads": list(leads),
            "figsize": list(figsize) if figsize else None,
        },
        "traces": [{"type": "heatmap_grid"}],
        "style": {
            "template": "weather_skills",
            "fontsize": fontsize,
            "colormap": colormap or field_scale.get("name"),
        },
        "title": fig_title,
        "geo": geo_out,
    }
    return write_plot_outputs(
        fig, resolved, output, datasets=named, dump_spec_path=dump_spec_dest(dump_spec)
    )


if __name__ == "__main__":
    plot_verify()

# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@plotting-refactor",
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
from weather_skills_core.cf import cf_dim, resolve_input_variable
from weather_skills_core.display_labels import (
    combine_display_labels,
    dataset_display_label,
    resolve_input_labels,
)
from weather_skills_core.plot import export
from weather_skills_core.plot.figure import (
    DEFAULT_FONTSIZE,
    parse_figsize,  # noqa: F401 — tests call this via the skill module
    format_plot_date_range,
)
from weather_skills_core.plot.maps import extent_from_da, slice_bbox_mask
from weather_skills_core.plot.spec import (
    DUMP_SPEC_ARGUMENT_HELP,
    SPEC_ARGUMENT_HELP,
    SPEC_VERSION,
    facet_with_spacing,
    maybe_emit_spec,
    named_datasets_from_spec,
    normalize_spec,
    overlay_spec,
    params_from_spec,
    parse_plot_spec,
    spec_inputs_from_datasets,
    spec_role_datasets,
    trace_at,
)
from weather_skills_core.plot.theme import (
    aggregation_days,
    is_precip,
    widest_precip_window,
)
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


def _pick_variable(ds, inputs, input_id, role):
    """This dataset's own ``inputs[].variable`` (by id), else ``inputs[0].variable``,
    else auto-detect — never a name borrowed from a *different* dataset's field."""
    name = resolve_input_variable(inputs, ds, id=input_id)
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
def plot_verify(
    obs,
    forecast,
    verify,
    output,
    spec=None,
    dump_spec=None,
    **kwargs,
):
    """Lead-week verification grid from obs, forecast, and pre-computed verify Zarrs."""
    user = spec.to_dict() if spec is not None else {}
    named_spec = named_datasets_from_spec(spec) if spec is not None else {}
    if obs is None:
        obs = named_spec.get("obs")
    forecasts = _as_list(forecast)
    if not forecasts:
        forecasts = spec_role_datasets(named_spec, "forecast")
    verify_sets = _as_list(verify)
    if not verify_sets:
        verify_sets = spec_role_datasets(named_spec, "verify")
    if obs is None:
        raise UsageError("pass --obs, or --spec with an obs input.")
    if not forecasts:
        raise UsageError("expected at least one --forecast, or --spec with forecast inputs.")
    if len(verify_sets) != len(forecasts):
        raise UsageError(
            f"--verify was passed {len(verify_sets)} time(s) but --forecast was passed "
            f"{len(forecasts)} time(s); pass one --verify per --forecast."
        )
    named = {
        "obs": obs,
        **{f"forecast{i}": fc for i, fc in enumerate(forecasts, start=1)},
        **{f"verify{i}": ds for i, ds in enumerate(verify_sets, start=1)},
    }
    internal_inputs = spec_inputs_from_datasets(named)
    internal = {
        "version": SPEC_VERSION,
        "skill": "plot-verify",
        "inputs": internal_inputs,
        "traces": [{"kind": "grid", "input": "obs"}],
        "layout": {"facet": {"rows": 2, "columns": 1 + len(forecasts)}},
        "theme": {"template": "weather_skills"},
    }
    spec_data = normalize_spec(overlay_spec(internal, user))
    params = params_from_spec(spec_data)
    trace0 = trace_at(spec_data)
    title, colormap = params["title"], params["colormap"]
    bbox, mask_geojson = params["bbox"], params["mask_geojson"]
    if isinstance(bbox, list):
        bbox = tuple(bbox)
    figsize = tuple(params["figsize"]) if params["figsize"] else None
    fontsize = (spec_data.get("theme") or {}).get("fontsize") or DEFAULT_FONTSIZE
    leads = list(trace0.get("leads") or [])
    if leads and len(leads) != len(forecasts):
        raise UsageError(
            f"traces[].leads has {len(leads)} entries but --forecast was passed "
            f"{len(forecasts)} time(s); pass one lead per forecast."
        )
    if not leads:
        leads = [f"{i}-week lead" for i in range(1, len(forecasts) + 1)]
    label = [
        item.get("label")
        for item in spec_data.get("inputs") or []
        if isinstance(item, dict) and not str(item.get("id", "")).startswith("verify")
    ]
    if not any(label):
        label = None
    labels = _as_list(label) or None
    leads, forecasts, verify_sets, labels = _order_week1_first(
        leads, forecasts, verify_sets, labels
    )
    named = {
        "obs": obs,
        **{f"forecast{i}": fc for i, fc in enumerate(forecasts, start=1)},
        **{f"verify{i}": ds for i, ds in enumerate(verify_sets, start=1)},
    }
    metrics = [_metric_from_verify(ds, f"--verify {i + 1}") for i, ds in enumerate(verify_sets)]
    if len(set(metrics)) != 1:
        raise UsageError(f"all --verify inputs must share the same verify_metric; got {metrics}.")
    metric = metrics[0]
    row_labels = _row_labels(obs, forecasts, metric, labels=labels)
    trace0 = spec_data["traces"][0]
    trace0["metric"] = metric
    trace0["leads"] = list(leads)

    if maybe_emit_spec(spec_data, dump_spec, datasets=named):
        return None
    if output is None:
        raise UsageError("--output is required unless --dump-spec is set")

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np

    inputs_spec = spec_data.get("inputs")
    obs_name = _pick_variable(obs, inputs_spec, "obs", "--obs")
    fc_names = [
        _pick_variable(fc, inputs_spec, f"forecast{i + 1}", f"--forecast {i + 1}")
        for i, fc in enumerate(forecasts)
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
    from weather_skills_core.plot.maps import (
        blank_cell,
        compile_grid,
        error_scale,
        heatmap_cell,
        hits_scale,
        scale_from_da,
    )

    field_cmap = colormap
    if colormap is None and is_precip(obs_da):
        field_cmap = widest_precip_window(
            _aggregation_days(obs_da),
            *(_aggregation_days(fc_da) for _label, fc_da, *_rest in columns),
        )
    field_scale = scale_from_da(obs_da, field_cmap, stretch=False, label=_variable_label(obs_da))
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
            obs_da, field_cmap, stretch=True, label=_variable_label(obs_da), vmin=vmin, vmax=vmax
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

    spec_for_grid = overlay_spec(
        {
            "layout": {
                "colorbar": {"location": "bottom", "thickness": 12, "pad": 0.03},
            }
        },
        spec_data,
    )
    compiled = compile_grid(
        [top_row, bottom_row],
        extent=extent,
        title=fig_title,
        col_titles=col_titles,
        row_titles=[row_labels[0], row_labels[2]],
        fontsize=fontsize,
        figsize=figsize,
        scales={"field": field_scale, "verify": verify_scale},
        spec=spec_for_grid,
    )
    named = {
        "obs": obs,
        **{f"forecast{i}": fc for i, fc in enumerate(forecasts, start=1)},
        **{f"verify{i}": ds for i, ds in enumerate(verify_sets, start=1)},
    }
    inputs = spec_inputs_from_datasets(named)
    name_by_id = {"obs": obs_name, **{f"forecast{i}": n for i, n in enumerate(fc_names, start=1)}}
    for item in inputs:
        key = str(item["id"])
        if key.startswith("forecast"):
            item["role"] = "forecast"
        elif key.startswith("verify"):
            item["role"] = "verify"
        else:
            item["role"] = "obs"
        resolved_name = name_by_id.get(key, obs_name)
        if resolved_name:
            item["variable"] = resolved_name
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
        "version": SPEC_VERSION,
        "skill": "plot-verify",
        "inputs": inputs,
        "layout": {
            "facet": facet_with_spacing(spec_data, rows=2, columns=1 + n_leads),
            "figsize": list(figsize) if figsize else None,
        },
        "traces": [{"kind": "grid", "metric": metric, "leads": list(leads)}],
        "theme": {
            "template": "weather_skills",
            "fontsize": fontsize,
            "colormap": colormap or field_scale.get("name"),
        },
        "title": fig_title,
        "geo": geo_out,
    }
    compiled.spec = {**compiled.spec, **resolved}
    return export(
        compiled,
        output,
        datasets=named,
        spec=spec_data,
    )


if __name__ == "__main__":
    plot_verify()

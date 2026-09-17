# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@plot-refactor",
#   "cf-xarray",
#   "cftime",
#   "matplotlib>=3.8",
#   "numpy",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""ECMWF-style mediogram: forecast vs m-climate ensemble distributions at a point."""

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.figure import DEFAULT_FONTSIZE, parse_figsize, resolve_axis_label
from weather_skills_core.units import (
    precip_for_display,
    to_standard_units,
    variable_label_for_display,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_resolve_axis_label = resolve_axis_label


def _select_point(da, lat, lon):
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if lat_dim is None or lon_dim is None:
        raise ValueError(f"Could not identify latitude/longitude in dims {list(da.dims)}.")
    return da.sel({lat_dim: lat, lon_dim: lon}, method="nearest")


@weather_skill(
    name="plot-mediogram",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lat", type=float, required=True, help="Point latitude.")
@weather_skill.argument("--lon", type=float, required=True, help="Point longitude.")
@weather_skill.argument("--title", default=None, help="Optional plot title.")
@weather_skill.argument(
    "--xlabel",
    default=None,
    help="Override the x-axis label (default: Forecast step).",
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
    help="Figure size W,H inches (e.g. 10,6 or 10x6). Default 10,5.",
)
def plot_mediogram(
    ds, variable, lat, lon, title, xlabel, ylabel, fontsize, figsize, output, **kwargs
):
    """ECMWF-style mediogram: forecast vs m-climate ensemble distributions at a point."""
    if len(ds) != 2:
        raise UsageError(f"expected exactly two --input paths, got {len(ds)}")
    ds_fc, ds_mc = ds
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np

    variable = variable or auto_variable(ds_fc)
    if variable is None or variable not in ds_fc or variable not in ds_mc:
        raise UsageError(
            f"variable '{variable}' must exist in both inputs. "
            f"forecast: {list(ds_fc.data_vars)}  mclimate: {list(ds_mc.data_vars)}"
        )

    ds_fc = precip_for_display(to_standard_units(ds_fc, variables=[variable]), variable)
    ds_mc = precip_for_display(to_standard_units(ds_mc, variables=[variable]), variable)
    da_fc = ds_fc[variable]
    da_mc = ds_mc[variable]

    for label, da in (("forecast", da_fc), ("mclimate", da_mc)):
        if "number" not in da.dims or "step" not in da.dims:
            raise UsageError(
                f"{label} input requires 'number' and 'step' dims; got {list(da.dims)}."
            )

    pt_fc = _select_point(da_fc, lat, lon)
    pt_mc = _select_point(da_mc, lat, lon)

    n_steps = min(pt_fc.sizes["step"], pt_mc.sizes["step"], 6)
    if n_steps < 1:
        raise DataError("no overlapping steps to plot.")

    pt_fc = pt_fc.isel(step=slice(0, n_steps)).transpose("number", "step")
    pt_mc = pt_mc.isel(step=slice(0, n_steps)).transpose("number", "step")
    fc = pt_fc.values
    mc = pt_mc.values

    lat_dim = cf_dim(pt_fc, "latitude")
    lon_dim = cf_dim(pt_fc, "longitude")
    snapped_lat = float(pt_fc[lat_dim].values) if lat_dim else lat
    snapped_lon = float(pt_fc[lon_dim].values) if lon_dim else lon

    step_vals = np.asarray(pt_fc["step"].values)
    tick_labels = []
    for value in step_vals:
        arr = np.asarray(value)
        if arr.dtype.kind == "m":
            tick_labels.append(f"+{int(arr.astype('timedelta64[D]').astype(int))}d")
        elif arr.dtype.kind == "M" or hasattr(value, "year"):
            if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
                tick_labels.append(
                    f"{int(value.year):04d}-{int(value.month):02d}-{int(value.day):02d}"
                )
            else:
                tick_labels.append(
                    str(np.datetime_as_string(arr.astype("datetime64[D]"), unit="D"))
                )
        else:
            tick_labels.append(str(value))
    qty = variable_label_for_display(pt_fc, fallback=variable, include_units=False)
    from weather_skills_core.plot_export import write_plot_outputs
    from weather_skills_core.plot_recipes import compile_mediogram
    from weather_skills_core.plot_spec import spec_inputs_from_datasets

    fig = compile_mediogram(
        fc,
        mc,
        tick_labels,
        title=title or f"Mediogram: {qty} at lat={snapped_lat:g}, lon={snapped_lon:g}",
        xlabel=_resolve_axis_label(xlabel, "Forecast step"),
        ylabel=_resolve_axis_label(ylabel, variable_label_for_display(pt_fc, fallback=variable)),
        fontsize=fontsize,
        figsize=figsize,
    )
    named = {"forecast": ds_fc, "mclimate": ds_mc}
    resolved = {
        "version": 1,
        "skill": "plot-mediogram",
        "inputs": spec_inputs_from_datasets(named),
        "traces": [{"type": "mediogram"}],
        "style": {"template": "weather_skills", "fontsize": fontsize},
        "geo": {"lat": snapped_lat, "lon": snapped_lon},
        "title": title,
    }
    return write_plot_outputs(fig, resolved, output, datasets=named)


if __name__ == "__main__":
    plot_mediogram()

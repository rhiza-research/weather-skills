# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
# ]
# ///
"""Compute a standardized anomaly, (field − mean) / std, from a Zarr and its climatology."""

import pint
import pint_xarray
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import ALIASES, detect_spatial_dims
from weather_skills_core.units import ureg

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="standardize-anomaly",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--climatology", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append", required=True)
def standardize_anomaly(ds, climatology, variable, **kwargs):
    """Compute a standardized anomaly, (field − mean) / std, from a Zarr and its climatology."""
    import xarray as xr

    # Put both sides' spatial dims on one name (core rounds their values on open).
    try:
        ds_lat, ds_lon = detect_spatial_dims(ds)
        clim_lat, clim_lon = detect_spatial_dims(climatology)
    except UsageError:
        ds_lat = clim_lat = None
    if ds_lat and clim_lat:
        ds = ds.rename({ds_lat: "latitude", ds_lon: "longitude"})
        climatology = climatology.rename({clim_lat: "latitude", clim_lon: "longitude"})

    vars_ = list(dict.fromkeys(variable))
    missing_input = [v for v in vars_ if v not in ds.data_vars]
    missing_clim = [
        f"{v}_{stat}"
        for v in vars_
        for stat in ("avg", "std")
        if f"{v}_{stat}" not in climatology.data_vars
    ]
    if missing_input or missing_clim:
        parts = []
        if missing_input:
            parts.append(f"--input missing {missing_input} (have {list(ds.data_vars)})")
        if missing_clim:
            parts.append(
                f"--climatology missing {missing_clim} (have {list(climatology.data_vars)})"
            )
        raise UsageError("; ".join(parts))

    out = {}
    for v in vars_:
        avg_name, std_name = f"{v}_avg", f"{v}_std"
        field, avg, std = ds[v], climatology[avg_name], climatology[std_name]
        units = [da.pint.units for da in (field, avg, std)]
        if all(u is not None for u in units):
            try:
                avg = avg.pint.to(units[0])
                # std is a spread: convert it as a difference, never with an
                # offset (a 1 degree_Celsius or delta_degC std is 1 K, not 274.15 K).
                std_step = ureg.Quantity(1, units[2]) - ureg.Quantity(0, units[2])
                field_step = ureg.Quantity(1, units[0]) - ureg.Quantity(0, units[0])
                scale = std_step.to(field_step.units).magnitude
            except (pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup) as exc:
                raise UsageError(
                    f"cannot standardize {v!r} against {avg_name!r}/{std_name!r}: {exc}. "
                    "Run unit-convert first."
                ) from None
            field, avg = field.pint.dequantify(), avg.pint.dequantify()
            std = std.pint.dequantify() * scale
        elif any(u is not None for u in units):
            raise UsageError(f"{v!r}, {avg_name!r} and {std_name!r} must all carry units or none")

        # A forecast's lead axis would otherwise align on init time, scoring
        # every lead against the init day's climatology.
        clim_dims = set(avg.dims) | set(std.dims)
        leads = [d for d in field.dims if ALIASES.get(d) == "prediction_timedelta"]
        if any(d not in clim_dims for d in leads):
            raise UsageError(
                f"{v!r}: --input has lead dim(s) {leads} the climatology lacks; "
                "run step-to-time on --input first so it aligns on valid time"
            )
        extra = [d for d in clim_dims if d not in field.dims]
        if extra:
            raise UsageError(
                f"{v!r}: --climatology has dim(s) {extra} that --input lacks, which would "
                "broadcast --input across them; select one entry of the climatology first"
            )

        anomaly = (field - avg) / std
        # No spread (e.g. a never-wet window): the z-score is undefined.
        anomaly = anomaly.where(std > 0)
        short = [
            f"{d} ({anomaly.sizes[d]} of {field.sizes[d]})"
            for d in field.dims
            if anomaly.sizes[d] < field.sizes[d]
        ]
        if short:
            raise UsageError(
                f"{v!r}: --climatology matches only some --input labels along {short}; "
                "fetch the climatology over the input's full window and grid, or clip-region "
                "the input to it (sub-daily input: aggregate-temporal to daily first)"
            )
        anomaly.attrs = {"units": "1", "long_name": f"{v} standardized anomaly"}
        out[f"{v}_anomaly"] = anomaly

    return xr.Dataset(out)


if __name__ == "__main__":
    standardize_anomaly()

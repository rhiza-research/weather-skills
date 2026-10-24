# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Convert data variables in a Zarr to target units or to the standard units."""

import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    STANDARD,
    classify_variable,
    convert_dataarray,
    kind_from_units,
    to_standard_units,
    units_equal,
    ureg,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

# Precip rate standard_name by target dimensionality, so any depth rate
# (mm day-1, mm hr-1, m s-1) or mass flux gets the matching CF name. Amounts
# (mm, kg m-2) get theirs from core on write.
_PRECIP_STANDARD_NAME_BY_DIMS = {
    ureg.Unit("mm day-1").dimensionality: STANDARD["precip"]["standard_name"],
    ureg.Unit("kg m-2 s-1").dimensionality: "precipitation_flux",
}


@weather_skill(
    name="unit-convert",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--to-units",
    help="Target units (UDUNITS/CF). Mutually exclusive with --to-standard.",
)
@weather_skill.argument(
    "--to-standard",
    action="store_true",
    help="Convert recognized temp/precip vars to degree_Celsius / mm day-1 / mm.",
)
@weather_skill.argument(
    "--standard-name",
    help="CF standard_name override (only with --to-units).",
)
def unit_convert(ds, variable, to_units, to_standard, standard_name, **kwargs):
    """Convert data variables in a Zarr to target units or to the standard units."""
    if to_standard and to_units:
        raise UsageError("pass only one of --to-units or --to-standard")
    if not to_standard and not to_units:
        raise UsageError("pass --to-units or --to-standard")
    if to_standard and standard_name is not None:
        raise UsageError("--standard-name is only valid with --to-units")
    if standard_name is not None and not standard_name.strip():
        raise UsageError("--standard-name must not be empty")
    if variable and variable not in ds.data_vars:
        raise UsageError(f"--variable {variable!r} not in dataset (have {list(ds.data_vars)})")
    names = [variable] if variable else list(ds.data_vars)

    if to_standard:
        skipped = [
            name
            for name in names
            if variable_units(ds[name]) is None
            or classify_variable(
                name,
                units=variable_units(ds[name]),
                standard_name=ds[name].attrs.get("standard_name"),
            )
            is None
        ]
        if variable and skipped:
            raise UsageError(
                f"variable {variable!r} is not a recognized temp/precip variable with units; "
                "pass --to-units instead"
            )
        if skipped:
            print(f"Left unchanged (not recognized temp/precip): {skipped}", file=sys.stderr)
        return to_standard_units(ds, variables=names)

    try:
        ureg.Unit(to_units)
    except Exception:  # noqa: BLE001 — any pint parse failure is a usage error
        raise UsageError(f"--to-units {to_units!r} is not a recognized unit") from None
    # Write the standard spelling (mm day-1, not mm/day or millimeter / day).
    out_units = next(
        (spec["units"] for spec in STANDARD.values() if units_equal(to_units, spec["units"])),
        to_units,
    )
    out = ds.copy()
    for name in names:
        da = ds[name]
        # Inputs open with pint units on the data, not in attrs.
        if da.pint.units is not None:
            da = da.pint.dequantify()
        src_units = da.attrs.get("units")
        if not (isinstance(src_units, str) and src_units.strip()):
            raise UsageError(f"variable '{name}' has no units attr")
        if units_equal(src_units, to_units):
            converted_da, density_converted = da, False
        else:
            try:
                converted_da, density_converted = convert_dataarray(da, to_units)
            except UsageError as e:
                hint = ""
                if {kind_from_units(src_units), kind_from_units(to_units)} == {
                    "precip",
                    "precip_amount",
                }:
                    hint = (
                        "; rate <-> amount needs a time period, which unit conversion "
                        "does not supply (rate to period total: convert-to-totals; "
                        "cumulative amount to rate: deaccumulate)"
                    )
                raise UsageError(
                    f"could not convert units for variable '{name}' "
                    f"({src_units!r} -> {to_units!r}): {e}{hint}"
                ) from None

        source_name = da.attrs.get("standard_name")
        if standard_name is not None:
            new_name = standard_name
        else:
            looked = _PRECIP_STANDARD_NAME_BY_DIMS.get(ureg.Unit(to_units).dimensionality)
            if isinstance(source_name, str):
                precip = "precipitation" in source_name.lower() or "rainfall" in source_name.lower()
            else:
                # No standard_name: only a precip-named variable (precip, tp, ...).
                kind = classify_variable(name, units=src_units)
                precip = kind in ("precip", "precip_amount")
            if looked is not None and precip:
                new_name = looked
            elif density_converted:
                new_name = None
            else:
                new_name = source_name
        attrs = {**converted_da.attrs, "units": out_units}
        if new_name is None:
            attrs.pop("standard_name", None)
        else:
            attrs["standard_name"] = new_name
        out[name] = converted_da
        out[name].attrs = attrs
    return out


if __name__ == "__main__":
    unit_convert()

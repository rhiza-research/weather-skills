# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "pandas",
# ]
# ///
"""Convert precipitation rates in a time-aggregated Zarr into period totals in mm."""

import re
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    PRECIP_AMOUNT_LONG_NAME,
    STANDARD,
    assert_nonoverlapping_intervals,
    classify_variable,
    coverage_values,
    filter_min_coverage,
    format_cell_methods,
    looks_like_rate_display_name,
    rate_to_total,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"


def _resolve_dim(ds, time_dim):
    import cf_xarray  # noqa: F401

    if time_dim:
        if time_dim not in ds.dims:
            raise UsageError(f"--time-dim {time_dim!r} not in dataset (have {list(ds.dims)})")
        return time_dim
    try:
        cf_time = ds.cf["time"].name
    except KeyError:
        cf_time = "time" if "time" in ds.dims else None
    if cf_time is not None and cf_time in ds.dims:
        if ds.sizes[cf_time] == 1 and "step" in ds.dims:
            return "step"
        return cf_time
    if "step" in ds.dims:
        return "step"
    return None


def _sum_cell_methods_dim(da, dim: str | None) -> str:
    """Dim name for ``cell_methods: …: sum`` after conversion."""
    if dim:
        return dim
    methods = da.attrs.get("cell_methods")
    if isinstance(methods, str):
        for token in ("time", "step"):
            if f"{token}:" in methods:
                return token
    return "time"


@weather_skill(
    name="convert-to-totals",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--min-coverage",
    type=float,
    default=1.0,
    help=(
        "Drop intervals whose aggregation_coverage is below this (0–1). Default 1.0; "
        "lower only when a task explicitly wants estimated totals from incomplete bins."
    ),
)
@weather_skill.argument("--time-dim")
def convert_to_totals(ds, variable, min_coverage, time_dim, **kwargs):
    """Convert precipitation rates in a time-aggregated Zarr into period totals in mm."""
    dim = _resolve_dim(ds, time_dim)
    kinds = {
        name: classify_variable(
            name, units=variable_units(da), standard_name=da.attrs.get("standard_name")
        )
        for name, da in ds.data_vars.items()
    }
    if variable is None:
        names = [name for name, kind in kinds.items() if kind == "precip"]
        if not names:
            raise UsageError(f"no precipitation rate variables in dataset (have {list(kinds)})")
    else:
        names = list(dict.fromkeys(variable))
        for name in names:
            if name not in kinds:
                raise UsageError(f"variable {name!r} not in dataset (have {list(kinds)})")
            if kinds[name] not in ("precip", "precip_amount"):
                raise UsageError(
                    f"variable {name!r} is not a precipitation rate; convert-to-totals "
                    "only converts precipitation"
                )

    import numpy as np
    import pandas as pd

    # After select collapses the only time/step sample, dims are spatial-only.
    # aggregation_period on the variable is enough for the multiply; select
    # keeps that bin's aggregation_coverage as a scalar coord, so the coverage
    # gate still applies. The overlap gate needs a time axis.
    recovery = (
        "Incomplete intervals are dropped, not scaled up. Observations "
        "usually end before the period does: fetch a window that ends on or "
        "before the latest available day (e.g. resolve-time <query> --as-of "
        "<the fetcher's --probe-latest day>). A forecast init may not reach "
        "that lead yet."
    )
    if dim is None:
        # Scalar after select; along e.g. a model dim after concat of selections.
        cov = ds.coords.get(AGGREGATION_COVERAGE_COORD)
        if cov is not None and (np.asarray(cov.values) < min_coverage - 1e-12).any():
            raise UsageError(
                f"the selected interval has aggregation_coverage "
                f"{np.array2string(np.asarray(cov.values), precision=3)}, below "
                f"--min-coverage {min_coverage}. {recovery}"
            )
    else:
        cov = coverage_values(ds, dim)
        if cov.size == ds.sizes[dim]:
            low = cov < min_coverage - 1e-12
            labels = [str(v) for v in pd.Index(np.asarray(ds[dim].values)[low])]
            dropped = ", ".join(
                f"{lab} ({c:.3g})" for lab, c in zip(labels[:10], cov[low][:10], strict=True)
            )
            dropped += ", ..." if low.sum() > 10 else ""
            if low.all():
                raise UsageError(
                    f"no {dim} intervals meet --min-coverage {min_coverage} "
                    f"(coverage: {dropped}). {recovery}"
                )
            if low.any():
                print(
                    f"Dropped {int(low.sum())} of {cov.size} {dim} interval(s) below "
                    f"--min-coverage {min_coverage}: {dropped}",
                    file=sys.stderr,
                )
        ds = filter_min_coverage(ds, dim, min_coverage)
    out = ds.copy(deep=False)
    for name in names:
        da = ds[name]
        period = da.attrs.get(AGGREGATION_PERIOD_ATTR)
        if not (isinstance(period, str) and period.strip()):
            raise UsageError(
                f"variable {name!r} has no {AGGREGATION_PERIOD_ATTR!r}; "
                "run aggregate-temporal first"
            )
        cell_methods = str(da.attrs.get("cell_methods", ""))
        if re.search(r":\s*(maximum|minimum)\b", cell_methods):
            raise UsageError(
                f"variable {name!r} is an extreme, not a mean rate "
                f"(cell_methods {cell_methods!r}); only a mean rate x period is a total"
            )
        if dim is not None:
            assert_nonoverlapping_intervals(ds, dim, period)
        total = rate_to_total(da, period, dim=dim if dim in da.dims else None)
        plain = total.pint.dequantify()
        attrs = {**da.attrs, **plain.attrs}
        attrs["units"] = STANDARD["precip_amount"]["units"]
        attrs["standard_name"] = STANDARD["precip_amount"]["standard_name"]
        attrs["long_name"] = PRECIP_AMOUNT_LONG_NAME
        # Product rate descriptions ("Average precipitation rate…", "prate",
        # step_type "avg") no longer describe a period total.
        attrs.pop("comment", None)
        attrs.pop("short_name", None)
        attrs.pop("step_type", None)
        if looks_like_rate_display_name(attrs.get("GRIB_name")):
            attrs["GRIB_name"] = PRECIP_AMOUNT_LONG_NAME
        attrs["cell_methods"] = format_cell_methods(_sum_cell_methods_dim(da, dim), "sum")
        # Keep aggregation_period so plotters can crop the nested precip palette.
        out[name] = plain
        out[name].attrs = attrs
    if AGGREGATION_COVERAGE_COORD in out.coords:
        partial = int((out[AGGREGATION_COVERAGE_COORD] < 1).sum())
        if partial == 0:
            out = out.drop_vars(AGGREGATION_COVERAGE_COORD)
        else:
            # Kept so the output records which bins are estimates.
            print(
                f"WARNING: {partial} incomplete bin(s) converted (--min-coverage "
                f"{min_coverage}); their missing samples are filled with the bin's mean "
                f"rate, so those totals are estimates, not observed totals. See "
                f"{AGGREGATION_COVERAGE_COORD!r} in the output.",
                file=sys.stderr,
            )
    return out


if __name__ == "__main__":
    convert_to_totals()

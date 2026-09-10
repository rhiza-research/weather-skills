# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime>=1.6",
#   "numpy",
#   "xarray",
# ]
# ///
"""Rainy season onset date along a time-like dim, via a chosen --definition.

For each selected data variable, finds the first day satisfying an onset
criterion along the time dim (``time``, or a lead-time dim such as ``step``)
and returns that day's own coordinate value (a duration if the dim is a
lead-time axis, an absolute date if it is already ``time``). Data variables
that don't carry the time dim pass through untouched.

Two onset definitions are supported via ``--definition``:

- ``ICPAC`` — a wet spell (``--wet-spell-days`` days totaling more than
  ``--wet-spell-thresh``) with no disqualifying dry spell
  (``--dry-spell-days`` or more consecutive days below ``--dry-spell-thresh``)
  within the following ``--search-days`` days.
- ``CHC_start_grow_season`` — the Climate Hazards Center two-window
  definition: the first day where the following ``--period1-days`` days
  accumulate at least ``--period1-thresh``, and the ``--period2-days`` days
  immediately after that accumulate more than ``--period2-thresh``.
"""

import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import ALIASES, PREDICTION_TIMEDELTA, detect_time_dim
from weather_skills_core.units import units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"


def _rainfall_onset_nd(block, wet_thresh, wet_days, dry_thresh, dry_days, search_days):
    """ICPAC onset search, vectorized over every leading (batch) dim at once.

    block : ndarray, shape (..., n_time) — time dim must be the last axis,
        daily rainfall accumulation (same units as wet_thresh/dry_thresh)

    Returns the 0-based time index of the onset day per gridpoint/member/etc
    (float, NaN where no qualifying onset was found within the series),
    shape (...).
    """
    import numpy as np

    n_time = block.shape[-1]
    dry = block < dry_thresh

    onset_idx = np.full(block.shape[:-1], np.nan)
    found = np.zeros(block.shape[:-1], dtype=bool)

    # last candidate start day that still leaves room for both the wet-spell
    # window and the full dry-spell search window
    t_max = n_time - max(wet_days, search_days)
    for t in range(0, t_max + 1):
        wet_window = block[..., t : t + wet_days]
        wet_sum = wet_window.sum(axis=-1)
        wet_nan = np.isnan(wet_window).any(axis=-1)
        candidate = (wet_sum > wet_thresh) & ~wet_nan

        # max consecutive dry-day run within the next search_days days
        counter = np.zeros(block.shape[:-1])
        max_run = np.zeros(block.shape[:-1])
        for w in range(search_days):
            counter = (counter + 1) * dry[..., t + w]
            np.maximum(max_run, counter, out=max_run)
        dry_nan = np.isnan(block[..., t : t + search_days]).any(axis=-1)
        no_dry_spell = (max_run < dry_days) & ~dry_nan

        qualifies = candidate & no_dry_spell & ~found
        onset_idx = np.where(qualifies, t, onset_idx)
        found = found | qualifies

    return onset_idx


def _rainfall_onset_accum_nd(block, period1_days, period1_thresh, period2_days, period2_thresh):
    """CHC_start_grow_season onset search, vectorized over every leading
    (batch) dim at once.

    block : ndarray, shape (..., n_time) — time dim must be the last axis,
        daily rainfall accumulation (same units as period1_thresh/period2_thresh)

    Returns the 0-based time index of the onset day per gridpoint/member/etc
    (float, NaN where no qualifying onset was found within the series),
    shape (...).
    """
    import numpy as np

    n_time = block.shape[-1]
    total_window = period1_days + period2_days

    onset_idx = np.full(block.shape[:-1], np.nan)
    found = np.zeros(block.shape[:-1], dtype=bool)

    # last candidate start day that still leaves room for both windows
    t_max = n_time - total_window
    for t in range(0, t_max + 1):
        first_window = block[..., t : t + period1_days]
        first_sum = first_window.sum(axis=-1)
        first_nan = np.isnan(first_window).any(axis=-1)

        second_window = block[..., t + period1_days : t + total_window]
        second_sum = second_window.sum(axis=-1)
        second_nan = np.isnan(second_window).any(axis=-1)

        qualifies = (
            (first_sum >= period1_thresh)
            & (second_sum > period2_thresh)
            & ~first_nan
            & ~second_nan
            & ~found
        )
        onset_idx = np.where(qualifies, t, onset_idx)
        found = found | qualifies

    return onset_idx


def _onset_idx_to_date(onset_idx, time_values):
    """Turn a float index-into-time-dim (NaN where no onset found) into
    actual onset values, using the time dim's own coordinate values (a
    duration for a lead-time dim like ``step``, an absolute date for
    ``time``)."""
    import numpy as np
    import xarray as xr

    idx = onset_idx.values
    valid = ~np.isnan(idx)
    idx_int = np.where(valid, idx, 0).astype(int)
    nat = np.array("NaT", dtype=time_values.dtype)
    onset_values = np.where(valid, time_values[idx_int], nat)
    return xr.DataArray(
        onset_values, dims=onset_idx.dims, coords=onset_idx.coords, name="onset_date"
    )


def _resolve_time_dim(ds, override):
    """Explicit --time-dim wins; else the ontology's time dim; else a
    lead-time dim (e.g. `step`, aliased to `prediction_timedelta`)."""
    if override:
        if override not in ds.dims:
            raise UsageError(f"--time-dim '{override}' not in dims {list(ds.dims)}")
        return override
    try:
        return detect_time_dim(ds)
    except UsageError:
        pass
    lead = next((d for d in ds.dims if ALIASES.get(d) == PREDICTION_TIMEDELTA), None)
    if lead is not None:
        print(
            f"Note: no time dim found; computing onset date over lead-time "
            f"dim '{lead}' instead. Pass --time-dim to override.",
            file=sys.stderr,
        )
        return lead
    raise UsageError(
        f"no time/lead-time dim identified in {list(ds.dims)}. Pass --time-dim to override."
    )


@weather_skill(
    name="onset-date",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help="Restrict the computation to this data variable. Repeatable. "
    "Each selected variable must carry the time dim. Default (unset): "
    "every data variable carrying the time dim.",
)
@weather_skill.argument(
    "--definition",
    required=True,
    choices=["ICPAC", "CHC_start_grow_season"],
    help="Onset criterion. 'ICPAC': wet-spell-then-no-dry-spell (see "
    "--wet-spell-* / --dry-spell-* / --search-days). "
    "'CHC_start_grow_season': two-window cumulative rainfall check (see "
    "--period1-* / --period2-*).",
)
@weather_skill.argument(
    "--wet-spell-thresh",
    type=float,
    default=20.0,
    help="[ICPAC] Total rainfall a wet spell must exceed, in the variable's own units.",
)
@weather_skill.argument(
    "--wet-spell-days",
    type=int,
    default=3,
    help="[ICPAC] Number of consecutive days summed for the wet-spell check.",
)
@weather_skill.argument(
    "--dry-spell-thresh",
    type=float,
    default=1.0,
    help="[ICPAC] A day below this rainfall (in the variable's own units) counts as dry.",
)
@weather_skill.argument(
    "--dry-spell-days",
    type=int,
    default=7,
    help="[ICPAC] A dry run of this many consecutive days or more disqualifies the onset.",
)
@weather_skill.argument(
    "--search-days",
    type=int,
    default=21,
    help="[ICPAC] Window (from the wet spell's first day) searched for a disqualifying dry spell.",
)
@weather_skill.argument(
    "--period1-days",
    type=int,
    default=10,
    help="[CHC_start_grow_season] Length of the first accumulation window, in days.",
)
@weather_skill.argument(
    "--period1-thresh",
    type=float,
    default=20.0,
    help="[CHC_start_grow_season] The first window must accumulate at least this much rainfall.",
)
@weather_skill.argument(
    "--period2-days",
    type=int,
    default=20,
    help="[CHC_start_grow_season] Length of the second (confirmation) accumulation window, in days.",
)
@weather_skill.argument(
    "--period2-thresh",
    type=float,
    default=20.0,
    help="[CHC_start_grow_season] The second window must accumulate more than this much rainfall.",
)
@weather_skill.argument(
    "--time-dim",
    default=None,
    help="Name of the time-like dim when not auto-detectable.",
)
def onset_date(
    ds,
    variable,
    definition,
    wet_spell_thresh,
    wet_spell_days,
    dry_spell_thresh,
    dry_spell_days,
    search_days,
    period1_days,
    period1_thresh,
    period2_days,
    period2_thresh,
    time_dim,
    **kwargs,
):
    """Rainy season onset date along a time-like dim, via a chosen --definition."""
    import numpy as np
    import xarray as xr

    dim = _resolve_time_dim(ds, time_dim)

    # Variable selection, mirroring `spell-length`: explicit --variable names
    # must be data variables and must each carry the time dim. Default
    # selection takes every data variable carrying it; the rest pass through
    # untouched.
    if variable is not None:
        data_vars = list(ds.data_vars)
        invalid = [v for v in variable if v not in ds.data_vars]
        if invalid:
            raise UsageError(
                f"--variable {invalid} not data variable(s) of the input. "
                f"Valid data variables: {data_vars}"
            )
        selected = list(dict.fromkeys(variable))
        missing = [v for v in selected if dim not in ds[v].dims]
        if missing:
            raise UsageError(f"variable(s) {missing} do not carry time dim '{dim}'.")
    else:
        selected = [v for v in ds.data_vars if dim in ds[v].dims]
        if not selected:
            raise UsageError(f"no data variable carries time dim '{dim}'.")

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through unreduced data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    if definition == "ICPAC":
        print(
            f"Computing onset date dim={dim} definition=ICPAC "
            f"wet_spell_thresh={wet_spell_thresh} wet_spell_days={wet_spell_days} "
            f"dry_spell_thresh={dry_spell_thresh} dry_spell_days={dry_spell_days} "
            f"search_days={search_days} variables={selected}",
            file=sys.stderr,
        )
    else:
        print(
            f"Computing onset date dim={dim} definition=CHC_start_grow_season "
            f"period1_days={period1_days} period1_thresh={period1_thresh} "
            f"period2_days={period2_days} period2_thresh={period2_thresh} "
            f"variables={selected}",
            file=sys.stderr,
        )

    time_values = ds[dim].values
    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        # The decorator opens data-variable inputs as pint quantities; bare
        # float thresholds can't compare against one inside apply_ufunc, so
        # drop back to a plain array (this also restores the string `units`
        # attr for the label).
        if getattr(da, "pint", None) is not None and da.pint.units is not None:
            da = da.pint.dequantify()

        if definition == "ICPAC":
            onset_idx = xr.apply_ufunc(
                _rainfall_onset_nd,
                da,
                input_core_dims=[[dim]],
                kwargs=dict(
                    wet_thresh=wet_spell_thresh,
                    wet_days=wet_spell_days,
                    dry_thresh=dry_spell_thresh,
                    dry_days=dry_spell_days,
                    search_days=search_days,
                ),
                dask="parallelized",
                dask_gufunc_kwargs={"allow_rechunk": True},
                output_dtypes=[np.float64],
            )
        else:
            onset_idx = xr.apply_ufunc(
                _rainfall_onset_accum_nd,
                da,
                input_core_dims=[[dim]],
                kwargs=dict(
                    period1_days=period1_days,
                    period1_thresh=period1_thresh,
                    period2_days=period2_days,
                    period2_thresh=period2_thresh,
                ),
                dask="parallelized",
                dask_gufunc_kwargs={"allow_rechunk": True},
                output_dtypes=[np.float64],
            )

        result = _onset_idx_to_date(onset_idx, time_values)

        src_units = da.attrs.get("units", "")
        # units_equal compares pint-equivalence rather than exact spelling,
        # so a dimensionless "1" and "dimensionless" are both recognized as
        # "no real unit" (see spell-length for the same reasoning).
        is_dimensionless = bool(src_units) and units_equal(src_units, "1")
        unit_suffix = f" {src_units}" if src_units and not is_dimensionless else ""

        if definition == "ICPAC":
            label = (
                f"{var} onset date (ICPAC: {wet_spell_thresh}{unit_suffix}/"
                f"{wet_spell_days}d, dry<{dry_spell_thresh}{unit_suffix} for "
                f"{dry_spell_days}d in {search_days}d)"
            )
            description = (
                f"first day of a {wet_spell_days}-day wet spell with "
                f">{wet_spell_thresh}{unit_suffix} total rainfall, with no dry spell of "
                f">={dry_spell_days} consecutive days (<{dry_spell_thresh}{unit_suffix}/day) "
                f"in the following {search_days} days"
            )
        else:
            label = (
                f"{var} onset date (CHC_start_grow_season: "
                f"{period1_days}d>={period1_thresh}{unit_suffix}, "
                f"{period2_days}d>{period2_thresh}{unit_suffix})"
            )
            description = (
                f"first day where the following {period1_days} days accumulate "
                f">={period1_thresh}{unit_suffix} rainfall, and the {period2_days} days "
                f"after that accumulate >{period2_thresh}{unit_suffix}"
            )

        # Attrs are rebuilt from scratch, NOT carried over from the source
        # variable: the source's standard_name/long_name/units describe the
        # input rainfall quantity, not this derived date/duration.
        # standard_name is explicitly None (CF has no entry for "rainy
        # season onset date" regardless). No `units` attr is set at all —
        # unlike spell-length's dimensionless-count output, this result is
        # genuinely timedelta64/datetime64-typed, and xarray's CF time coder
        # insists on owning that dtype's `units` attr itself; a manually-set
        # (or attrs-healed) `units` string on a time-like variable raises at
        # write time regardless of its value. The result is written under a
        # new variable name (replacing `var`, not reusing its name like
        # spell-length does) specifically so the decorator's same-name
        # attrs-healing from the source variable never applies here and
        # re-introduces `units` (see the naming note just below).
        result.attrs = {
            "GRIB_name": label,
            "long_name": label,
            "description": description,
            "standard_name": None,
        }
        del out_ds[var]
        # Sandwiched, not `{var}_onset_date` / `onset_date_{var}`:
        # weather_skills_core classifies a variable's physical kind (and
        # whether it then requires a `units` attr) by whether its name
        # starts or ends with a short hint like "tp", "pr", "t2m", "tas" —
        # exactly the source variable names this skill is typically run on.
        # A prefix or suffix placement would carry that hint to either end
        # of the new name and misclassify this date output as precip/temp
        # (which *does* require units), making it unreadable as `--input` to
        # any other skill. Sandwiching `var` between fixed, non-hint text
        # keeps it out of both boundary positions regardless of what `var`
        # is named.
        out_ds[f"onset_{var}_date"] = result

    # The collapsed dim disappears from the output (with its coordinates)
    # once no data variable carries it; a dim still carried by a
    # pass-through variable stays.
    if dim in out_ds.dims and all(dim not in out_ds[v].dims for v in out_ds.data_vars):
        out_ds = out_ds.drop_dims(dim)

    return out_ds


if __name__ == "__main__":
    onset_date()

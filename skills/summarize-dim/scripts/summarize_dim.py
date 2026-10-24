# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
# ]
# ///
"""Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""

import datetime
import itertools
import re
import sys

import numpy as np
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims, names_for
from weather_skills_core.standard_utils import latitude_weights
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    data_interval_of,
    infer_timestep,
    parse_aggregation_period,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"

_TIME_LABELS = ("month", "year", "dayofyear", "month_group")
_INITIALS = "JFMAMJJASOND"
# Grid and station dims a sum must not add up (latitude, lon, station_id, ...).
_SPATIAL_DIMS = frozenset((*names_for("lat"), *names_for("lon"), *names_for("point_id")))


@weather_skill(
    name="summarize-dim",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dim",
    action="append",
    help="Dim to summarize (repeatable).",
)
@weather_skill.argument(
    "--by",
    action="append",
    help="Label coord to group by; its dims collapse to one value per label (repeatable).",
)
@weather_skill.argument(
    "--method",
    required=True,
    choices=["mean", "std", "min", "max", "sum", "median"],
)
@weather_skill.argument(
    "--lat-weighted",
    action="store_true",
    help="cos(lat) weights for --method mean over latitude.",
)
@weather_skill.argument(
    "--min-coverage",
    type=float,
    help=(
        "With a --by time label: blank values whose group has data for less than this share "
        "(0-1) of its full calendar period. Default 1.0."
    ),
)
def summarize_dim(ds, variable, dim, by, method, lat_weighted, min_coverage, **kwargs):
    """Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""
    if not dim and not by:
        raise UsageError("pass --dim, --by, or both")
    dims = list(dict.fromkeys(dim or []))
    for d in dims:
        if d not in ds.dims:
            raise UsageError(f"--dim {d!r} not in dataset (have {list(ds.dims)})")
    by = list(dict.fromkeys(by or []))
    for b in by:
        if b not in ds.coords or b in ds.dims:
            labels = [c for c in ds.coords if c not in ds.dims]
            raise UsageError(f"--by {b!r} is not a label coord (have {labels})")
    if by and lat_weighted:
        raise UsageError("--lat-weighted cannot be combined with --by")
    time_by = [b for b in by if "time" in ds[b].dims]
    if min_coverage is not None and not time_by:
        raise UsageError(
            f"--min-coverage needs a --by label along time ({', '.join(_TIME_LABELS)})"
        )
    min_coverage = 1.0 if min_coverage is None else min_coverage
    if not 0.0 <= min_coverage <= 1.0:
        raise UsageError(f"--min-coverage must be in [0, 1]; got {min_coverage}")
    for b in time_by:
        if b not in _TIME_LABELS or ds[b].dims != ("time",):
            raise UsageError(
                f"--by {b!r} runs along time but is not a label-group time label "
                f"({', '.join(_TIME_LABELS)}), so its full calendar period is unknown"
            )
    # Read before the label coords on time are dropped below.
    seasons = ds["month_group"].values if "month_group" in ds.coords and time_by else None
    weight = None
    if AGGREGATION_COVERAGE_COORD in ds.coords and ds[AGGREGATION_COVERAGE_COORD].dims == ("time",):
        # An input interval that was itself 3/7 complete counts as 3/7 of a step.
        weight = np.nan_to_num(np.asarray(ds[AGGREGATION_COVERAGE_COORD].values, dtype=float))
    # The dims a label spans collapse into one entry per label.
    group_dims = list(dict.fromkeys(d for b in by for d in ds[b].dims))
    dims = list(dict.fromkeys([*group_dims, *dims]))
    # Other label coords on the grouped dims (admin_1_name, ...) cannot survive the collapse.
    ds = ds.drop_vars(
        [c for c in ds.coords if c not in (*by, *ds.dims) and set(ds[c].dims) & set(group_dims)]
    )
    # Groups, built once for every variable. Labels on the same dims are matched together;
    # labels on different dims (a county and a year) combine as a product. Each group keeps
    # only the rows/columns/times it touches, plus a mask when that box holds other cells,
    # so no step copies more than one group.
    label_values = {}
    groups = []
    for dset in dict.fromkeys(ds[b].dims for b in by):
        names = [b for b in by if ds[b].dims == dset]
        for b in by:
            if b not in names and set(ds[b].dims) & set(dset):
                raise UsageError(f"--by {b!r} and {names[0]!r} share some but not all dims")
        for b in names:
            values = np.unique(ds[b].values)
            label_values[b] = values[values != ""] if values.dtype.kind in "UO" else values
            if label_values[b].size == 0:
                raise DataError(f'--by {b!r}: every value is the empty label ""; nothing to group')
        entries = []
        for combo in itertools.product(*(range(label_values[b].size) for b in names)):
            mask = np.ones([ds.sizes[d] for d in dset], dtype=bool)
            for b, i in zip(names, combo, strict=True):
                mask &= ds[b].values == label_values[b][i]
            if not mask.any():
                continue
            index = [
                np.nonzero(mask.any(axis=tuple(a for a in range(mask.ndim) if a != axis)))[0]
                for axis in range(mask.ndim)
            ]
            box = mask[np.ix_(*index)]
            entries.append(
                (
                    dict(zip(names, combo, strict=True)),
                    dict(zip(dset, index, strict=True)),
                    None if box.all() else xr.DataArray(box, dims=dset),
                    None,  # expected time steps, filled in below for the time group
                )
            )
        if not entries:
            raise DataError(
                f"--by {', '.join(names)}: no cell or step has a non-empty value of every one "
                "of these labels at once; nothing to group"
            )
        groups.append(entries)

    # Coverage: for each time group, the steps a complete record would hold. The labels are
    # rebuilt on a gap-free axis at the data's step reaching a year past both ends; a group
    # expects every step of each calendar run (one MAM, one year) the data touch, so a
    # season cut by the start or end of the record is incomplete.
    if time_by:
        times = ds.indexes["time"]
        if not times.is_unique:
            repeated = times[times.duplicated()][0]
            raise UsageError(
                f"time {repeated} appears more than once, so its values would count twice "
                "toward a --by group; drop the repeated steps first"
            )
        first = times[0]
        period = next(
            (
                ds[v].attrs[AGGREGATION_PERIOD_ATTR]
                for v in ds.data_vars
                if isinstance(ds[v].attrs.get(AGGREGATION_PERIOD_ATTR), str)
            ),
            None,
        ) or data_interval_of(ds)
        calendar = re.fullmatch(r"\s*(\d+)\s*(month|year)s?\s*", period or "")
        if calendar or (period or "").strip() == "1 dekad":
            # Calendar steps: labels must be month starts (dekads on days 1, 11, 21).
            every = (
                int(calendar.group(1)) * (12 if calendar.group(2) == "year" else 1)
                if calendar
                else 1
            )
            n = (times[-1].year - first.year) * 12 + times[-1].month - first.month
            full = [
                first.replace(
                    year=first.year + (first.month - 1 + k) // 12,
                    month=(first.month - 1 + k) % 12 + 1,
                    day=day,
                )
                for k in range(-24, n + 25)
                if k % every == 0
                for day in ((1,) if calendar else (1, 11, 21))
            ]
            longer_than_a_day = True
        else:
            step = parse_aggregation_period(period) if period else infer_timestep(ds, "time")
            step = datetime.timedelta(seconds=float(step.to("second").magnitude))
            margin = int(np.ceil(datetime.timedelta(days=366) / step))
            n = round((times[-1] - first) / step)
            full = [first + i * step for i in range(-margin, n + margin + 1)]
            longer_than_a_day = step > datetime.timedelta(days=1)
        full = (
            pd.DatetimeIndex(full) if isinstance(times, pd.DatetimeIndex) else xr.CFTimeIndex(full)
        )
        at = full.get_indexer(times)
        if (at < 0).any():
            raise UsageError(
                f"time values do not fall on a regular {period or 'time'} axis, so the steps "
                "each --by group should hold cannot be counted"
            )
        # Labels at each step's start, and at its last second to catch a step (a week
        # from 27 May) that runs into the next group.
        labelled = []
        for index in (full, full[1:] - datetime.timedelta(seconds=1)):
            month = np.asarray(index.month)
            season = np.full(index.size, "", dtype=object)
            begins_last_year = np.zeros(index.size, dtype=bool)
            for token in np.unique(seasons[seasons != ""]) if seasons is not None else []:
                starts = [
                    start
                    for start in range(12)
                    if len(token) <= 12
                    and all(_INITIALS[(start + i) % 12] == c for i, c in enumerate(token))
                ]
                if len(starts) == 1:  # anything else fails the check below
                    months = [(starts[0] + i) % 12 + 1 for i in range(len(token))]
                    season[np.isin(month, months)] = token
                    begins_last_year |= np.isin(month, months) & (month < months[0])
            labelled.append(
                {
                    "month": month,
                    "dayofyear": np.asarray(index.dayofyear),
                    # As label-group: a group crossing December takes the year it begins.
                    "year": np.asarray(index.year) - begins_last_year,
                    "month_group": season.astype(str),
                }
            )
        rebuilt, rebuilt_end = labelled
        change = np.zeros(full.size, dtype=bool)
        grouped = np.ones(full.size, dtype=bool)
        for b in time_by:
            if not np.array_equal(rebuilt[b][at], ds[b].values):
                raise UsageError(
                    f"--by {b!r} does not hold the calendar {b} of each time step, as "
                    "label-group writes it, so its full calendar period is unknown"
                )
            # A step of a day or less belongs to the day it is stamped on.
            crossing = rebuilt[b][at] != rebuilt_end[b][at]
            if longer_than_a_day and crossing.any():
                raise UsageError(
                    f"the {period or 'time'} step starting {times[np.argmax(crossing)]} runs "
                    f"into the next --by {b} group, so the group would take in time outside "
                    "it; aggregate to steps that fit inside the groups (daily, or monthly for "
                    "months and month groups)"
                )
            change[1:] |= rebuilt[b][1:] != rebuilt[b][:-1]
            if b == "month_group":
                grouped &= rebuilt[b] != ""
        run = np.cumsum(change)
        touched = np.isin(run, run[at]) & grouped
        if touched[0] or touched[-1]:
            raise UsageError("a --by time group lasts over a year, so its full period is unknown")
        t = list(dict.fromkeys(ds[b].dims for b in by)).index(("time",))
        groups[t] = [
            (
                labels,
                isel,
                box,
                int(
                    (
                        touched
                        & np.logical_and.reduce(
                            [rebuilt[b] == label_values[b][i] for b, i in labels.items()]
                        )
                    ).sum()
                ),
            )
            for labels, isel, box, _ in groups[t]
        ]

    lat_dim = None
    if lat_weighted:
        if method != "mean":
            raise UsageError("--lat-weighted requires --method mean")
        lat_dim, _ = detect_spatial_dims(ds)
        if lat_dim not in dims:
            raise UsageError(f"--lat-weighted needs --dim {lat_dim}")

    # No --variable: whole dataset (vars without the dim are skipped by rdims).
    selected = list(dict.fromkeys(variable)) if variable else list(ds.data_vars)
    for var in selected:
        if var not in ds.data_vars:
            raise UsageError(f"--variable {var!r} not in dataset (have {list(ds.data_vars)})")
    out = ds.drop_vars(by)
    for var in selected:
        da = ds[var]
        rdims = [d for d in dims if d in da.dims]
        # With --by, only variables carrying every label dim are summarized.
        if not rdims or not set(group_dims) <= set(da.dims):
            continue
        units = da.pint.units
        over_time = [d for d in rdims if d in ("time", "step")]
        if method == "sum" and over_time and units is not None:
            if dict(units.dimensionality).get("[time]", 0) < 0:
                raise UsageError(
                    f"--method sum over {', '.join(over_time)} would add up {var!r} rates "
                    f"({da.attrs.get('units', units)}) into a rate, not a total; convert the "
                    "rates to period totals first (convert-to-totals)"
                )
        # Adding up cells or stations of a physical quantity (mm, degC) grows with the
        # area and means nothing; dimensionless counts and flags add up to how many.
        over_space = [d for d in rdims if d in _SPATIAL_DIMS]
        if method == "sum" and over_space and (units is None or units.dimensionality):
            spatial_by = [b for b in by if set(ds[b].dims) & set(over_space)]
            via = f" (via --by {', '.join(spatial_by)})" if spatial_by else ""
            raise UsageError(
                f"--method sum over {', '.join(over_space)}{via} would add up {var!r} "
                f"({'no units' if units is None else format(units, '~')}) across cells or stations; use "
                "--method mean for a regional value, after summing over time in a "
                "separate call. Only dimensionless variables (units 1) sum over space"
            )
        if by:
            # min_count=1 / ddof=1 as below. Combinations with no cells stay NaN.
            kw = {"min_count": 1} if method == "sum" else {"ddof": 1} if method == "std" else {}
            kept = [d for d in da.dims if d not in rdims]
            da = da.pint.dequantify()  # values are filled into numpy; units go back on below
            values = None
            dropped = 0
            for parts in itertools.product(*groups):
                index = {d: i for _, isel, _, _ in parts for d, i in isel.items()}
                sub = da.isel(index)
                for _, _, box, _ in parts:
                    if box is not None:
                        sub = sub.where(box)
                if time_by:
                    # Coverage is per input cell (station, grid cell, member):
                    # a cell missing too many of the group's calendar steps is
                    # left out before any spatial reduction, so cells missing
                    # different days never add up to a complete group.
                    present = sub.notnull()
                    if weight is not None:
                        present = present * xr.DataArray(weight[index["time"]], dims="time")
                    expected = next(n for _, _, _, n in parts if n is not None)
                    cell_share = present.sum("time") / expected
                    complete = cell_share >= min_coverage - 1e-12
                    dropped += int(((cell_share > 0) & ~complete).sum())
                    sub = sub.where(complete)
                    others = [d for d in rdims if d != "time"]
                    # The least-covered cell that went into each value.
                    share = cell_share.where(complete)
                    share = share.min(others) if others else share
                reduced = getattr(sub, method)(dim=rdims, keep_attrs=True, **kw)
                if values is None:
                    template = reduced
                    shape = [da.sizes[d] for d in kept] + [label_values[b].size for b in by]
                    values = np.full(shape, np.nan, dtype=np.result_type(reduced.dtype, np.float32))
                    coverage = np.full(shape, np.nan)
                where = {b: i for labels, _, _, _ in parts for b, i in labels.items()}
                values[(..., *(where[b] for b in by))] = reduced.transpose(*kept).values
                if time_by:
                    coverage[(..., *(where[b] for b in by))] = share.transpose(*kept).values
            coords = {
                **{c: v for c, v in template.coords.items() if set(v.dims) <= set(kept)},
                **{b: label_values[b] for b in by},
            }
            if dropped:
                print(
                    f"{var}: left out {dropped} cell-group(s) with data for less than "
                    f"--min-coverage {min_coverage:g} of the group's calendar time steps",
                    file=sys.stderr,
                )
            out[var] = xr.DataArray(values, dims=[*kept, *by], coords=coords, attrs=template.attrs)
            if time_by and method == "sum" and min_coverage < 1.0:
                # Kept so the output records which sums are estimates.
                partial = int((np.isfinite(values) & (coverage < 1.0 - 1e-12)).sum())
                if partial:
                    print(
                        f"WARNING: {partial} {var} sum(s) come from groups missing some time "
                        f"steps (--min-coverage {min_coverage:g}); missing steps are not filled, "
                        "so those sums are estimates from partial data and fall short of the "
                        f"full-period total. See {var}_coverage in the output.",
                        file=sys.stderr,
                    )
                out[f"{var}_coverage"] = xr.DataArray(
                    coverage,
                    dims=[*kept, *by],
                    coords=coords,
                    attrs={
                        "long_name": f"Share of each group's calendar time steps with {var} data",
                        "units": "1",
                    },
                )
                out[var].attrs["ancillary_variables"] = f"{var}_coverage"
            if units is not None:
                out[var] = out[var].copy(data=out[var].data * units)
        elif method == "sum":
            # min_count=1: all-NaN → NaN, not 0
            out[var] = da.sum(dim=rdims, keep_attrs=True, min_count=1)
        elif method == "std":
            out[var] = da.std(dim=rdims, keep_attrs=True, ddof=1)
        elif method == "mean" and lat_weighted and lat_dim in rdims:
            out[var] = (
                da.pint.dequantify()
                .weighted(latitude_weights(ds[lat_dim]))
                .mean(dim=rdims, keep_attrs=True)
            )
            if da.pint.units is not None:
                out[var] = out[var].copy(data=out[var].data * da.pint.units)
        else:
            out[var] = getattr(da, method)(dim=rdims, keep_attrs=True)
        # Record the statistic (CF cell_methods). A spread or a sum is not the
        # input quantity, so its long_name must not read "Total precipitation".
        attrs = dict(out[var].attrs)
        cf_method = {"std": "standard_deviation", "min": "minimum", "max": "maximum"}
        step = f"{': '.join(rdims)}: {cf_method.get(method, method)}"
        if by:
            step += f" (grouped by {', '.join(by)})"
        attrs["cell_methods"] = (
            f"{attrs['cell_methods']} {step}" if attrs.get("cell_methods") else step
        )
        if method in ("std", "sum"):
            label = "Standard deviation" if method == "std" else "Sum"
            what = attrs.get("long_name") or attrs.get("standard_name") or var
            attrs["long_name"] = f"{label} of {what} across {', '.join(rdims)}"
            # standard_name stays: CF encodes a spread as the quantity's
            # standard_name plus a cell_methods statistic.
            for key in ("GRIB_name", "short_name", "comment"):
                attrs.pop(key, None)
        out[var].attrs = attrs

    for d in dims:
        if d in out.dims and all(d not in out[v].dims for v in out.data_vars):
            out = out.drop_dims(d)
    return out


if __name__ == "__main__":
    summarize_dim()

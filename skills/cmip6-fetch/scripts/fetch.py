# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "gcsfs",
#   "pandas",
# ]
# ///
"""Fetch one CMIP6 climate-model run from the public Pangeo catalog as a weather-skills Zarr."""

import sys
from datetime import UTC, datetime

import cf_xarray  # noqa: F401  (fail-fast probe)
import cf_xarray.units  # noqa: F401  (fail-fast probe; configures pint CF registry)
import gcsfs
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs, udunits_error
from weather_skills_core.standard_utils import (
    bbox_subset,
    ensure_normalized_longitude,
    verify_cf_decode,
)
from weather_skills_core.units import (
    precip_amounts_to_rates,
    stamp_data_interval,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"

_CATALOG_URL = "https://storage.googleapis.com/cmip6/pangeo-cmip6.csv"
_CF_CONVENTIONS = "CF-1.13"


def _resolve_zstore(model, experiment, variable, member, table, grid) -> tuple:
    """Resolve facet flags against the CMIP6 catalog to exactly one zstore."""
    try:
        df = pd.read_csv(_CATALOG_URL)
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"failed to download or parse the CMIP6 catalog from {_CATALOG_URL} "
            f"({type(exc).__name__}: {exc}). Check network access to "
            "storage.googleapis.com, then retry."
        ) from None
    mask = (
        (df.source_id == model)
        & (df.experiment_id == experiment)
        & (df.variable_id == variable)
        & (df.member_id == member)
        & (df.table_id == table)
    )
    sub = df[mask]
    if grid:
        sub = sub[sub.grid_label == grid]
    if sub.empty:
        for_model = df[df.source_id == model]
        if for_model.empty:
            hint = f"unknown --model {model!r}; sample models: {sorted(df.source_id.unique())[:10]}"
        else:
            # Each list holds the other facets fixed, so the valid choice for
            # the one that is wrong is always shown in full.
            m = for_model
            hint = (
                f"for model {model!r}: experiments with {variable}/{table}="
                f"{sorted(m[(m.variable_id == variable) & (m.table_id == table)].experiment_id.unique())}; "
                f"variables in {experiment}/{table}="
                f"{sorted(m[(m.experiment_id == experiment) & (m.table_id == table)].variable_id.unique())}; "
                f"tables for {experiment}/{variable}="
                f"{sorted(m[(m.experiment_id == experiment) & (m.variable_id == variable)].table_id.unique())}; "
                f"members for {experiment}/{variable}/{table}="
                f"{sorted(m[(m.experiment_id == experiment) & (m.variable_id == variable) & (m.table_id == table)].member_id.unique())}"
            )
            if grid:
                hint += f"; grids={sorted(df[mask].grid_label.unique())}"
        raise UsageError(
            f"no CMIP6 dataset matches model={model} experiment={experiment} "
            f"variable={variable} member={member} table={table}"
            + (f" grid={grid}" if grid else "")
            + f".\n{hint}"
        )
    grids = sorted(sub.grid_label.unique())
    if len(grids) > 1:
        raise UsageError(f"multiple grid_labels match: {grids}. Pass --grid to choose one.")
    row = sub.sort_values("version").iloc[-1]
    zstore = row["zstore"]
    if not isinstance(zstore, str) or not zstore.strip():
        raise UsageError(
            f"the matched CMIP6 entry (model={model} experiment={experiment} "
            f"variable={variable} member={member} table={table} "
            f"grid={grids[0]} version={row['version']}) has no zstore path "
            "(the catalog row is empty/withdrawn). Try different facets or another "
            "--grid/--member."
        )
    return zstore, grids[0], str(row["version"])


def _open_remote(state, model, experiment, variable, member, table, grid) -> dict:
    """Resolve the catalog and open the matched store, at most once per run."""
    if "ds" not in state:
        zstore, grid_label, version = _resolve_zstore(
            model, experiment, variable, member, table, grid
        )
        state.update(zstore=zstore, grid_label=grid_label, version=version)
        fs = gcsfs.GCSFileSystem(token="anon")
        mapper = fs.get_mapper(zstore)
        try:
            time_coder = xr.coders.CFDatetimeCoder(use_cftime=True)
            ds = xr.open_zarr(mapper, consolidated=True, decode_times=time_coder)
        except AttributeError:
            ds = xr.open_zarr(mapper, consolidated=True, use_cftime=True)

        source_calendar = ds["time"].encoding.get("calendar")
        if source_calendar is None:
            raise DataError(
                "could not determine the source time calendar from the CMIP6 store; "
                "refusing to write a store whose calendar cannot be verified."
            )

        if "lat" not in ds.dims or "lon" not in ds.dims:
            raise UsageError(
                f"{model}/{variable} ({grid_label}) is not on a regular 1-D "
                f"lat/lon grid (dims {tuple(ds.dims)}); this fetcher handles only regular "
                "lat/lon grids."
            )
        state.update(
            ds=ds,
            source_calendar=source_calendar,
        )
    return state


def _drop_bounds(ds):
    """Drop *_bnds/*_bounds variables and orphaned bounds attrs/dims."""
    bounds_vars = [
        v for v in ds.variables if str(v).endswith("_bnds") or str(v).endswith("_bounds")
    ]
    if bounds_vars:
        ds = ds.drop_vars(bounds_vars)
    for dim_coord in ("bnds", "bounds", "nv"):
        if dim_coord in ds.coords and dim_coord not in ds.dims:
            ds = ds.drop_vars(dim_coord)
    for v in ds.variables:
        if "bounds" in ds[v].attrs:
            del ds[v].attrs["bounds"]
    return ds


@weather_skill(
    name="cmip6-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", required=True)
@weather_skill.argument("--model", required=True, help="CMIP6 source_id (e.g. GFDL-CM4).")
@weather_skill.argument(
    "--experiment", required=True, help="CMIP6 experiment_id (e.g. historical, ssp245)."
)
@weather_skill.argument("--member", default="r1i1p1f1", help="CMIP6 member_id (default r1i1p1f1).")
@weather_skill.argument("--table", default="Amon", help="CMIP6 table_id (default Amon).")
@weather_skill.argument(
    "--grid",
    help="CMIP6 grid_label; required only when more than one matches the other facets.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(
    start_time,
    end_time,
    bbox,
    model,
    experiment,
    variable,
    member,
    table,
    grid,
    probe_latest,
    **kwargs,
):
    """Fetch one CMIP6 climate-model run from the public Pangeo catalog as a weather-skills Zarr."""
    if probe_latest is not None:
        print("none")
        return

    state = _open_remote({}, model, experiment, variable, member, table, grid)
    ds = state["ds"]
    grid_label = state["grid_label"]
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()

    if variable not in ds.data_vars:
        raise UsageError(
            f"variable {variable!r} not present in the dataset; available: {sorted(ds.data_vars)}"
        )

    source_globals = dict(ds.attrs)
    # CMIP6's own cell edges, e.g. [Dec 1, Jan 1) for a mid-month Amon label.
    native_bounds = ds.get(ds["time"].attrs.get("bounds", "time_bnds"))
    ds = ds[[variable]]
    ds = ds.rename({"lat": "latitude", "lon": "longitude"})
    ds = _drop_bounds(ds)
    ds = ensure_normalized_longitude(ds)
    if bbox:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")

    # Compare dates as YYYYMMDD integers: a string slice parses the bounds in
    # the model calendar, so --end-time 2015-03-31 crashes on 360_day models
    # (and 2016-02-29 on noleap ones).
    start_key = start_time.year * 10000 + start_time.month * 100 + start_time.day
    end_key = end_time.year * 10000 + end_time.month * 100 + end_time.day
    if native_bounds is not None:
        # Refuse a window the run does not fully cover (e.g. historical ends
        # 2014-12-31) instead of silently returning the overlapping part.
        first, last = native_bounds.values[0, 0], native_bounds.values[-1, 1]
        first_key = first.year * 10000 + first.month * 100 + first.day
        last_key = last.year * 10000 + last.month * 100 + last.day
        if start_key < first_key or end_key >= last_key:
            raise UsageError(
                f"{model}/{experiment} covers {first.strftime('%Y-%m-%d')} up to (not "
                f"including) {last.strftime('%Y-%m-%d')}; the window {start_iso}..{end_iso} "
                "extends past it. Historical runs end 2014-12-31 and ssp* scenarios start "
                "2015-01-01: fetch each experiment's part separately and join them with "
                "the concat skill along time."
            )
    key = ds["time"].dt.year * 10000 + ds["time"].dt.month * 100 + ds["time"].dt.day
    ds = ds.isel(time=((key >= start_key) & (key <= end_key)).values)
    if ds.sizes.get("time", 0) == 0:
        raise DataError(
            f"{model}/{experiment}/{variable} has no data in "
            f"{start_iso}..{end_iso} (dataset time range may not cover the window)."
        )

    print(
        f"Fetching cmip6 {model}/{experiment}/{member}/{table}/"
        f"{variable}/{grid_label} {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    bbox_label = f"{bbox[0]}/{bbox[1]}/{bbox[2]}/{bbox[3]}" if bbox is not None else None
    history_line = (
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} cmip6-fetch: "
        f"subset {variable} to {start_iso}..{end_iso}"
        + (f" bbox {bbox_label}" if bbox_label else "")
        + f"; mapped onto the weather-skills standard dataset and re-stamped {_CF_CONVENTIONS}."
    )
    prior_history = source_globals.get("history", "")
    new_globals = dict(source_globals)
    new_globals["Conventions"] = _CF_CONVENTIONS
    new_globals["history"] = (
        (prior_history + "\n" + history_line) if prior_history else history_line
    )
    new_globals["weather_skills_source"] = (
        f"cmip6:{model}/{experiment}/{member}/{table}/{variable}/{grid_label}"
    )
    ds.attrs = new_globals

    stamp_cf_attrs(ds)

    units = ds[variable].attrs.get("units")
    if units is None or not str(units).strip():
        raise DataError(
            f"variable {variable!r} has no `units` attribute; cannot write a "
            "CF-compliant store. The source dataset is missing CF units."
        )
    units_exc = udunits_error(units)
    if units_exc is not None:
        raise DataError(
            f"variable {variable!r} has units {units!r}, which is not a valid "
            f"udunits string ({units_exc}); refusing to write it under a CF-1.13 claim."
        )

    verify_cf_decode(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds)
    ds = stamp_data_interval(ds)
    # Irregular axes (monthly tables) get CF bounds. Inferred ones treat each
    # mid-month label as a cell end ([Nov 16, Dec 16] for December), which
    # blends adjacent months when aggregated; use the source's edges instead.
    name = ds["time"].attrs.get("bounds")
    if name and native_bounds is not None:
        ds[name] = (ds[name].dims, native_bounds.sel(time=ds["time"]).values)
    return ds


if __name__ == "__main__":
    fetch()

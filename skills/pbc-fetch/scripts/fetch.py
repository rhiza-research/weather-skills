# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "xarray",
#   "zarr>=3",
#   "cftime",
#   "gcsfs",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a PBC AI Weather Quest precip forecast and write a weather-skills standard dataset."""

from __future__ import annotations

import re
import sys
from datetime import timedelta

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, ensure_normalized_longitude
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_BUCKET = "sheerwater-public-datalake"
_PREFIX = "pbc-data"
_DEFAULT_DATASET = "era5-p_pr_19"
_COMPACT_DATE_RE = re.compile(r"^\d{8}$")
_VAR_ALIASES = {"forecast": "pr", "precip": "pr", "tp": "pr", "probability": "pr"}

# Canonical folder names under gs://sheerwater-public-datalake/pbc-data/.
# lead_start is AI-WQ day-1-is-init numbering: week 3 is days 19–25, week 4 is 26–32.
# Store folders are named by that first valid day (init + lead_start - 1).
_DATASETS: dict[str, dict] = {
    "era5-p_pr_19": {"lead_start": 19, "period": 1},
    "era5-p_pr_26": {"lead_start": 26, "period": 2},
}
_ALIASES = {
    "pr_19": "era5-p_pr_19",
    "p1": "era5-p_pr_19",
    "pr_26": "era5-p_pr_26",
    "p2": "era5-p_pr_26",
}
_DATASET_CHOICES = [*_DATASETS, *_ALIASES]

_AUTH_MSG = (
    "cannot read gs://sheerwater-public-datalake/pbc-data. It is a public "
    "bucket (no credentials needed) — this is likely a network issue or the "
    "bucket/prefix no longer exists."
)

_FS = None


def _gcs():
    """Anonymous GCS filesystem: gs://sheerwater-public-datalake is public."""
    global _FS
    if _FS is None:
        import gcsfs

        try:
            _FS = gcsfs.GCSFileSystem(token="anon")
        except Exception as exc:  # noqa: BLE001 — surface access failures cleanly
            raise DataError(f"{_AUTH_MSG} ({exc})") from None
    return _FS


def _resolve_dataset(dataset: str) -> str:
    name = _ALIASES.get(dataset, dataset)
    if name not in _DATASETS:
        raise UsageError(
            f"unknown --dataset {dataset!r}; choose one of: {', '.join(_DATASET_CHOICES)}"
        )
    return name


def _lead_offset(dataset: str) -> int:
    """Days from init to the folder / first valid day of the weekly window."""
    return int(_DATASETS[dataset]["lead_start"]) - 1


def _compact(d) -> str:
    return d.strftime("%Y%m%d")


def _date_from_compact(compact: str):
    from datetime import date

    return date.fromisoformat(f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}")


def _store_key(dataset: str, folder: str) -> str:
    return f"{_PREFIX}/{dataset}/{folder}/{dataset}-{folder}.zarr"


def _gcs_path(key: str) -> str:
    return f"{_BUCKET}/{key.rstrip('/')}"


def _store_exists(key: str) -> bool:
    try:
        return bool(_gcs().exists(f"{_gcs_path(key)}/zarr.json"))
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"GCS HEAD failed for gs://{_BUCKET}/{key} ({exc}). {_AUTH_MSG}") from None


def _list_folder_dates(dataset: str) -> list[str]:
    prefix = f"{_BUCKET}/{_PREFIX}/{dataset}"
    try:
        entries = _gcs().ls(prefix, detail=False)
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"GCS listing failed for gs://{prefix} ({exc}). {_AUTH_MSG}") from None
    dates: list[str] = []
    for path in entries:
        name = str(path).rstrip("/").rsplit("/", 1)[-1]
        if _COMPACT_DATE_RE.fullmatch(name):
            dates.append(name)
    dates.sort()
    return dates


def _init_for_folder(dataset: str, folder: str):
    return _date_from_compact(folder) - timedelta(days=_lead_offset(dataset))


def _folder_for_init(dataset: str, init) -> str:
    return _compact(init + timedelta(days=_lead_offset(dataset)))


def _resolve_folder(date, dataset: str) -> tuple[str, object]:
    """Return ``(folder YYYYMMDD, init date)``.

    ``--date`` is the forecast init (matching other fetchers). If that folder
    is missing, the same string is tried as the store folder name (valid-week
    start) so a date copied from ``gsutil ls`` still works.
    """
    if date is not None:
        as_init_folder = _folder_for_init(dataset, date)
        if _store_exists(_store_key(dataset, as_init_folder)):
            return as_init_folder, date
        as_folder = _compact(date)
        if _store_exists(_store_key(dataset, as_folder)):
            return as_folder, _init_for_folder(dataset, as_folder)
        raise DataError(
            f"no {dataset!r} Zarr for --date {date.isoformat()} in "
            f"gs://{_BUCKET}/{_PREFIX}/{dataset}/ (looked for folder "
            f"{as_init_folder} as init+{_lead_offset(dataset)}d and {as_folder} "
            "as a valid-week folder). Omit --date to take the latest available, "
            "or pass --probe-latest."
        )

    folders = _list_folder_dates(dataset)
    if not folders:
        raise DataError(
            f"no YYYYMMDD folders found in gs://{_BUCKET}/{_PREFIX}/{dataset}/; "
            "the PBC prefix may be empty or unreachable."
        )
    for folder in reversed(folders):
        if _store_exists(_store_key(dataset, folder)):
            return folder, _init_for_folder(dataset, folder)
    raise DataError(
        f"no {dataset!r} Zarr found under gs://{_BUCKET}/{_PREFIX}/{dataset}/. "
        f"Available --dataset ids: {', '.join(_DATASET_CHOICES)}."
    )


def _open_remote(key: str):
    import xarray as xr

    path = _gcs_path(key)
    try:
        mapper = _gcs().get_mapper(path)
        return xr.open_zarr(mapper, consolidated=True)
    except DataError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"failed to open remote store gs://{path} ({exc}).") from None


def _as_forecast(ds, *, init, dataset: str):
    """Rename ``forecast`` → ``pr`` and add classic forecast ``time`` + ``step``."""
    import numpy as np

    if "forecast" in ds.data_vars and "pr" not in ds.data_vars:
        ds = ds.rename({"forecast": "pr"})
    if "pr" not in ds.data_vars:
        raise DataError(
            f"{dataset} store has no 'forecast'/'pr' variable "
            f"(have: {', '.join(sorted(ds.data_vars))})."
        )

    lead = np.timedelta64(_lead_offset(dataset), "D")
    init_ns = np.datetime64(init.isoformat(), "ns")
    ds = ds.expand_dims(step=[lead])
    ds = ds.assign_coords(
        time=init_ns,
        valid_time=("step", np.array([init_ns + lead])),
    )
    spatial = [d for d in ("quintile", "step", "latitude", "longitude") if d in ds.dims]
    others = [d for d in ds.dims if d not in spatial]
    if spatial:
        ds = ds.transpose(*others, *spatial)

    ds["pr"].attrs.update(
        units="1",
        long_name="precipitation quintile probability",
        standard_name="probability",
    )
    if "quintile" in ds.coords:
        ds["quintile"].attrs.setdefault("long_name", "quintile upper bound")
        ds["quintile"].attrs.setdefault("units", "1")
    ds["step"].attrs.update(
        standard_name="forecast_period",
        long_name="time since forecast_reference_time",
    )
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    if "valid_time" in ds.coords:
        ds["valid_time"].attrs.setdefault("standard_name", "time")
        ds["valid_time"].attrs.setdefault("long_name", "valid time of first day of forecast week")
    return ds


@weather_skill(
    name="pbc-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--dataset",
    default=_DEFAULT_DATASET,
    choices=_DATASET_CHOICES,
    help=(
        f"PBC product under gs://{_BUCKET}/{_PREFIX}/ "
        f"(default {_DEFAULT_DATASET}; week-3 days 19–25. "
        "era5-p_pr_26 / pr_26 / p2 is week-4 days 26–32)."
    ),
)
@weather_skill.argument("--date")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available init YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(era5-p_pr_19, pr_26, …)."
    ),
)
def fetch(dataset, date, bbox, variable, output, **kwargs):
    """Fetch a PBC AI Weather Quest precip forecast and write a weather-skills standard dataset.

    Opens a Zarr under
    ``gs://sheerwater-public-datalake/pbc-data/<dataset>/<YYYYMMDD>/`` (public,
    read anonymously). Optionally subsets by ``--bbox`` and ``--variable``,
    maps quintile probabilities onto a classic forecast (scalar init ``time``
    + ``step``), and returns a Dataset for the decorator to write.
    """
    dataset = _resolve_dataset(dataset)
    if kwargs.get("probe_latest") is not None:
        ident = kwargs["probe_latest"] or dataset
        dsid = _resolve_dataset(ident)
        _folder, init = _resolve_folder(None, dsid)
        print(init.isoformat())
        return

    folder, init = _resolve_folder(date, dataset)
    key = _store_key(dataset, folder)
    print(f"Resolved init date: {init.isoformat()}", file=sys.stderr)
    print(f"Opening gs://{_BUCKET}/{key}", file=sys.stderr)

    ds = _open_remote(key)
    ds = _as_forecast(ds, init=init, dataset=dataset)

    if variable:
        wanted = [_VAR_ALIASES.get(v, v) for v in variable]
        missing = [v for v in wanted if v not in ds.data_vars]
        if missing:
            raise UsageError(
                f"variable(s) not in {dataset}: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}"
            )
        ds = ds[wanted]

    if bbox is not None:
        ds = bbox_subset(ds, bbox)
    else:
        ds = ensure_normalized_longitude(ds)

    ds = ds.load()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"sheerwater-pbc:{dataset}",
        pbc_dataset=dataset,
        pbc_folder=folder,
    )
    stamp_cf_attrs(ds)
    return stamp_data_interval(ds, period="7 day")


if __name__ == "__main__":
    fetch()

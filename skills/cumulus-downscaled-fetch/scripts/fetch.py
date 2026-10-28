# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "adlfs",
#   "h5netcdf",
#   "h5py",
# ]
# ///
"""Fetch a Cumulus downscaled Aurora precipitation forecast for one init date as a weather-skills Zarr."""

from __future__ import annotations

import re
import sys

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, require_env
from weather_skills_core.units import (
    precip_amounts_to_rates,
    stamp_data_interval,
    stamp_precip_amounts,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_ACCOUNT = "italynorthdata"
_CONTAINER = "data"
_SAS_ENV = "AZURE_STORAGE_SAS_TOKEN"
# <_ROOT>/<init>/<model folder>/<checkpoint>/<target folder>/precip_24h/data/
#   YYYY-MM-DD-00-LLLL.nc                 ensemble mean, one file per 24 h lead
#   member_NNN/YYYY-MM-DD-00-LLLL.nc      individual members
_ROOT = (
    f"{_CONTAINER}/live_forecasts/downscaled_models/aurora/"
    "east_africa-ghana-kenya-senegal-west_africa"
)
_INIT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-(\d{4})\.nc$")
_MEMBER_RE = re.compile(r"^member_(\d+)$")

# --model name → model folder. Any other model folder name is also accepted.
_MODELS = {
    "diffusion": "model_diffusion_unet_precip_input_output_residual_shiftedlog_calibrated_silu_selfcond_precip24h_only",
    "flow-matching": "model_flow_matching_unet_precip_input_residual_shifted_log_precip24h_only",
    "unet": "model_unet_precip_output_residual_fs128",
}
# --target name → target folder: the precipitation product the model was trained to.
_TARGETS = {
    "era5": "total_precipitation_24h_acc_era5",
    "imerg": "total_precipitation_24h_acc_imerg",
}
_TP_ALIASES = {"tp", "precip", "precipitation", "total_precipitation", "precip_24h"}
DEFAULT_WORKERS = 16


def _storage_options() -> dict[str, str]:
    """adlfs options from the SAS in the env (raw query, leading ``?``, or full blob URL)."""
    (raw,) = require_env(
        _SAS_ENV,
        message=(
            f"{_SAS_ENV} must be set to a read SAS for Azure container "
            f"{_ACCOUNT}/{_CONTAINER} (list and read: sp=rl)."
        ),
    )
    token = raw.strip().strip("'\"").split("?", 1)[-1]
    if "sig=" not in token.lower():
        raise UsageError(
            f"{_SAS_ENV} is set but does not look like an Azure SAS "
            "(expected a query string containing sig=)."
        )
    return {"account_name": _ACCOUNT, "sas_token": token}


def _azure_error(exc: Exception, what: str) -> DataError:
    """Wrap an Azure failure as a DataError, with the SAS signature redacted."""
    # Also the URL-encoded form (sig%3D...) some clients echo in errors.
    text = re.sub(
        r"(sig(?:=|%3D))[^&\s'\"<>%]+(?:%[0-9A-F]{2}[^&\s'\"<>%]*)*",
        r"\1REDACTED",
        f"{type(exc).__name__}: {exc}",
        flags=re.I,
    )
    hint = ""
    if any(s in text.lower() for s in ("401", "403", "forbidden", "authenticat", "authoriz")):
        hint = f" Check {_SAS_ENV}: it needs list and read (sp=rl) on {_ACCOUNT}/{_CONTAINER}."
    return DataError(f"{what} ({text}).{hint}")


def _filesystem():
    options = _storage_options()  # check the SAS before importing adlfs
    import adlfs

    return adlfs.AzureBlobFileSystem(**options)


def _ls(path: str) -> list[str]:
    """Child names under ``path`` (empty when it does not exist)."""
    fs = _filesystem()
    try:
        paths = fs.ls(path, detail=False)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        raise _azure_error(exc, f"Azure listing failed for az://{_ACCOUNT}/{path}/") from None
    return sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in paths)


def _read_leads(paths: list[str], workers: int):
    """Open each lead NetCDF (one 24 h total) and return them in order."""
    import io
    from concurrent.futures import ThreadPoolExecutor

    import xarray as xr

    fs = _filesystem()

    def read(path):
        try:
            raw = fs.cat_file(path)
        except Exception as exc:  # noqa: BLE001
            raise _azure_error(exc, f"failed to read az://{_ACCOUNT}/{path}") from None
        with xr.open_dataset(io.BytesIO(raw), engine="h5netcdf") as ds:
            return ds.load()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(read, paths))


@weather_skill(
    name="cumulus-downscaled-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--model",
    default="diffusion",
    help=f"One of {', '.join(_MODELS)} (default diffusion), or a model folder name.",
)
@weather_skill.argument(
    "--target",
    default="era5",
    choices=list(_TARGETS),
    help="Precipitation product the model was trained to (default era5).",
)
@weather_skill.argument(
    "--checkpoint",
    help="Substring of the checkpoint folder to use when an init has several.",
)
@weather_skill.argument(
    "--members",
    action="store_true",
    help="Fetch the individual ensemble members instead of the ensemble mean.",
)
@weather_skill.argument("--date")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"Max concurrent file downloads (default {DEFAULT_WORKERS}).",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest init YYYY-MM-DD with output for --model / --target (or none) "
        "on stdout and exit. Optional IDENT selects a --model."
    ),
)
def fetch(
    model, target, checkpoint, members, date, bbox, variable, probe_latest, workers, **kwargs
):
    """Fetch a Cumulus downscaled Aurora precipitation forecast for one init date as a weather-skills Zarr."""
    # Reads the per-lead NetCDF files under
    # ``az://italynorthdata/data/live_forecasts/downscaled_models/aurora/<domain>/<init>/...``
    # with the SAS in ``AZURE_STORAGE_SAS_TOKEN``, optionally subsets by
    # ``--bbox``, and returns a Dataset for the decorator to write.
    import numpy as np
    import xarray as xr

    probe = probe_latest
    model_folder = _MODELS.get(probe or model, probe or model)
    target_folder = _TARGETS[target]
    if workers < 1:
        raise UsageError(f"--workers must be at least 1 (got {workers}).")
    unknown = set(variable or ()) - _TP_ALIASES
    if unknown:
        raise UsageError(
            f"variable(s) not in this Cumulus product: {', '.join(sorted(unknown))}.\nAvailable: tp"
        )

    def checkpoints(init):
        """Checkpoint folders under ``init`` that hold this target's output."""
        found = []
        for ckpt in _ls(f"{_ROOT}/{init}/{model_folder}"):
            if target_folder in _ls(f"{_ROOT}/{init}/{model_folder}/{ckpt}"):
                found.append(ckpt)
        if checkpoint:
            found = [c for c in found if checkpoint in c]
        return found

    inits = [i for i in _ls(_ROOT) if _INIT_RE.fullmatch(i)]
    if date is not None:
        inits = [i for i in inits if i == date.isoformat()]
    # Walk back from the newest init to the first that has this model's output.
    init = ckpts = None
    for candidate in reversed(inits):
        ckpts = checkpoints(candidate)
        if ckpts:
            init = candidate
            break

    if probe is not None:
        print(init or "none")
        return
    if init is None:
        wanted = f"init {date.isoformat()}" if date else "any init"
        raise DataError(
            f"no Cumulus downscaled output for model {model_folder!r}, target {target!r}"
            + (f", checkpoint matching {checkpoint!r}" if checkpoint else "")
            + f" at {wanted} under az://{_ACCOUNT}/{_ROOT}/. "
            "Pass --probe-latest to see the latest init with output, or try another --model."
        )
    ckpt = ckpts[-1]
    if len(ckpts) > 1:
        print(
            f"WARNING: init {init} has {len(ckpts)} checkpoints for {model_folder}: "
            f"{', '.join(ckpts)}. Using {ckpt}; pass --checkpoint to pick another.",
            file=sys.stderr,
        )
    data_dir = f"{_ROOT}/{init}/{model_folder}/{ckpt}/{target_folder}/precip_24h/data"
    print(f"Reading az://{_ACCOUNT}/{data_dir}/", file=sys.stderr)

    if members:
        member_dirs = [d for d in _ls(data_dir) if _MEMBER_RE.fullmatch(d)]
        if not member_dirs:
            raise DataError(
                f"no member_* folders under az://{_ACCOUNT}/{data_dir}/ "
                "(this model publishes only one field). Omit --members."
            )
    else:
        member_dirs = [None]

    fields = []
    for member in member_dirs:
        folder = f"{data_dir}/{member}" if member else data_dir
        files = [f for f in _ls(folder) if _FILE_RE.fullmatch(f)]
        if not files:
            raise DataError(f"no lead files under az://{_ACCOUNT}/{folder}/ (still publishing?).")
        leads = _read_leads([f"{folder}/{f}" for f in files], workers)
        da = xr.concat([ds["precip_24h"].squeeze("time", drop=True) for ds in leads], "step")
        # Each file is the 24 h total ending at init + lead; label it by the period start.
        hours = [int(_FILE_RE.fullmatch(f).group(1)) for f in files]
        da = da.assign_coords(
            step=np.array(hours, dtype="timedelta64[h]") - np.timedelta64(24, "h")
        )
        da = da.drop_vars([c for c in da.coords if c not in da.dims])
        if bbox is not None:
            da = bbox_subset(da, bbox)
        fields.append(da)

    if members:
        numbers = [int(_MEMBER_RE.fullmatch(d).group(1)) for d in member_dirs]
        tp = xr.concat(fields, "number", join="outer").assign_coords(number=numbers)
        tp = tp.transpose("number", "step", "latitude", "longitude")
    else:
        tp = fields[0].transpose("step", "latitude", "longitude")
    # A lead file that is not there stays NaN (never filled); steps end at the last file.
    tp = tp.sortby("step").sortby("latitude")
    days = np.arange(int(tp["step"].max() / np.timedelta64(1, "D")) + 1)
    tp = tp.reindex(step=(days * np.timedelta64(1, "D")).astype(tp["step"].dtype))
    empty = days[tp.isnull().all([d for d in tp.dims if d != "step"]).values]
    if len(days) < 46 or len(empty):
        print(
            f"WARNING: init {init} has {len(days) - len(empty)} of 46 daily leads"
            + (
                f"; step(s) {', '.join(map(str, empty))} d have no file (left NaN)"
                if len(empty)
                else ""
            )
            + ". Still publishing? Pass --date with an earlier init for every lead.",
            file=sys.stderr,
        )

    tp.attrs = {
        "units": "mm",
        "long_name": "24-hour total precipitation",
        "comment": (
            "24 h precipitation from init + step to init + step + 24 h "
            "(cumulus-downscaled-fetch relabelled file leads from period end to period start)."
        ),
    }
    ds = tp.to_dataset(name="tp").assign_coords(time=np.datetime64(init, "ns"))
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"cumulus-downscaled:{model_folder}/{ckpt}/{target_folder}",
        cumulus_downscaled_member_set="members" if members else "ensemble_mean",
    )
    stamp_cf_attrs(ds)
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval="1 day", deaccumulate=False)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()

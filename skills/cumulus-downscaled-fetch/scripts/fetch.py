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
# <_BASE>/<source>/<_DOMAIN>/<init>/<model folder>/<checkpoint>/<target folder>/precip_24h/data/
#   YYYY-MM-DD-00-LLLL.nc                 ensemble mean, one file per 24 h lead
#   member_NNN/YYYY-MM-DD-00-LLLL.nc      individual members
_BASE = f"{_CONTAINER}/live_forecasts/downscaled_models"
_DOMAIN = "east_africa-ghana-kenya-senegal-west_africa"
_INIT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-(\d{4})\.nc$")
_MEMBER_RE = re.compile(r"^member_(\d+)$")

# --model name → model folder prefix. Folder names differ between sources and
# setups, so the name is matched by prefix at each init. A full model folder
# name is also accepted.
_MODELS = {
    "diffusion": "model_diffusion_",
    "flow-matching": "model_flow_matching_",
    "unet": "model_unet_",
}
# --target name → target folder: the precipitation product the model was trained to.
_TARGETS = {
    "era5": "total_precipitation_24h_acc_era5",
    "imerg": "total_precipitation_24h_acc_imerg",
}
_TP_ALIASES = {"tp", "precip", "precipitation", "total_precipitation", "precip_24h"}
DEFAULT_WORKERS = 16


def _filesystem():
    """adlfs filesystem from the SAS in the env (raw query, leading ``?``, or full blob URL)."""
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
    import adlfs

    return adlfs.AzureBlobFileSystem(account_name=_ACCOUNT, sas_token=token)


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


@weather_skill(
    name="cumulus-downscaled-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--source",
    default="aurora",
    help="Subfolder of downscaled_models/ to read, e.g. aurora-imerg (default aurora).",
)
@weather_skill.argument(
    "--model",
    default="diffusion",
    help=f"One of {', '.join(_MODELS)} (default diffusion), or a model folder name.",
)
@weather_skill.argument(
    "--target",
    choices=list(_TARGETS),
    help=(
        "Precipitation product the model was trained to (default: the only one "
        "the checkpoint has, else era5)."
    ),
)
@weather_skill.argument(
    "--checkpoint",
    default="",
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
        "Print the latest init YYYY-MM-DD with output for --model (or none) "
        "on stdout and exit. Optional IDENT selects a --model."
    ),
)
def fetch(
    source,
    model,
    target,
    checkpoint,
    members,
    date,
    bbox,
    variable,
    probe_latest,
    workers,
    **kwargs,
):
    """Fetch a Cumulus downscaled Aurora precipitation forecast for one init date as a weather-skills Zarr."""
    # Reads the per-lead NetCDF files under
    # ``az://italynorthdata/data/live_forecasts/downscaled_models/<source>/<domain>/<init>/...``
    # with the SAS in ``AZURE_STORAGE_SAS_TOKEN``, optionally subsets by
    # ``--bbox``, and returns a Dataset for the decorator to write.
    import io
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    import xarray as xr

    model = probe_latest or model
    root = f"{_BASE}/{source.strip('/')}/{_DOMAIN}"
    if workers < 1:
        raise UsageError(f"--workers must be at least 1 (got {workers}).")
    unknown = set(variable or ()) - _TP_ALIASES
    if unknown:
        raise UsageError(
            f"variable(s) not in this Cumulus product: {', '.join(sorted(unknown))}.\nAvailable: tp"
        )
    fs = _filesystem()

    def ls(path):
        """Sorted child names under ``path`` (empty when it does not exist)."""
        try:
            paths = fs.ls(path, detail=False)
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            raise _azure_error(exc, f"Azure listing failed for az://{_ACCOUNT}/{path}/") from None
        return sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in paths)

    def model_folders(init):
        """Model folders at ``init`` that ``--model`` names (exact name or short-name prefix)."""
        prefix = _MODELS.get(model)
        return [m for m in ls(f"{root}/{init}") if m == model or (prefix and m.startswith(prefix))]

    def checkpoints(init):
        """(model folder, checkpoint folders) for ``--model`` at ``init``; ``(None, [])`` when absent."""
        folders = model_folders(init)
        if len(folders) > 1:
            raise DataError(
                f"init {init} under az://{_ACCOUNT}/{root}/ has {len(folders)} model folders "
                f"for --model {model}: {', '.join(folders)}. Pass the full folder name as --model."
            )
        if not folders:
            return None, []
        return folders[0], ls(f"{root}/{init}/{folders[0]}")

    def read(path):
        """One lead NetCDF: the 24 h total ending at init + lead."""
        try:
            raw = fs.cat_file(path)
        except Exception as exc:  # noqa: BLE001
            raise _azure_error(exc, f"failed to read az://{_ACCOUNT}/{path}") from None
        with xr.open_dataset(io.BytesIO(raw), engine="h5netcdf") as ds:
            return ds["precip_24h"].squeeze("time", drop=True).load()

    # Walk back from the newest init to the first that has this model.
    all_inits = [i for i in ls(root) if _INIT_RE.fullmatch(i)]
    inits = all_inits
    if date is not None:
        inits = [i for i in inits if i == date.isoformat()]
    init = None
    for candidate in reversed(inits):
        model_folder, ckpts = checkpoints(candidate)
        ckpts = [c for c in ckpts if checkpoint in c]
        if ckpts:
            init = candidate
            break

    if probe_latest is not None:
        print(init or "none")
        return
    if init is None:
        wanted = f"init {date.isoformat()}" if date else "any init"
        there = checkpoints(inits[0])[1] if date and inits else []
        models = ls(f"{root}/{inits[0]}") if date and inits else []
        if not all_inits:
            hint = (
                f"No init folders there: check --source (subfolders of az://{_ACCOUNT}/{_BASE}/: "
                f"{', '.join(ls(_BASE))})."
            )
        elif there:
            hint = (
                f"That init has only: {', '.join(there)}. Pass --checkpoint matching one of "
                "those; it is a different setup, so say so when comparing it with other inits."
            )
        elif models:
            hint = f"That init has model folders: {', '.join(models)}. Pass one as --model."
        else:
            hint = "Pass --probe-latest to see the latest init with output, or try another --model."
        raise DataError(
            f"no Cumulus downscaled output for model {model!r}"
            + (f", checkpoint matching {checkpoint!r}" if checkpoint else "")
            + f" at {wanted} under az://{_ACCOUNT}/{root}/. {hint}"
        )
    # Checkpoints at one init are different setups (sampler steps n20/n30,
    # guidance cfg2/cfg3, ensemble size) with different patterns. Without
    # --checkpoint, keep the one later inits carry on running.
    if len(ckpts) > 1 and not checkpoint:
        kept = ckpts
        for later in (i for i in all_inits if i > init):
            later_ckpts = checkpoints(later)[1]
            if later_ckpts:
                kept = [c for c in kept if c in later_ckpts]
                if len(kept) <= 1:
                    break
        if len(kept) == 1:
            print(
                f"WARNING: init {init} has {len(ckpts)} checkpoints for {model_folder}: "
                f"{', '.join(ckpts)}. Using {kept[0]}, the one init {later} still "
                "runs; pass --checkpoint to pick another.",
                file=sys.stderr,
            )
            ckpts = kept
    if len(ckpts) > 1:
        raise DataError(
            f"init {init} has {len(ckpts)} checkpoints for {model_folder}"
            + (f" matching {checkpoint!r}" if checkpoint else "")
            + f": {', '.join(ckpts)}. Pass --checkpoint with a substring that picks one."
        )
    (ckpt,) = ckpts
    print(f"Checkpoint: {ckpt}", file=sys.stderr)
    # Comparing setups is allowed, but say when this init is not on the live one.
    for newest in reversed(all_inits):
        newest_ckpts = checkpoints(newest)[1]
        if newest_ckpts:
            break
    if ckpt not in newest_ckpts:
        print(
            f"WARNING: checkpoint {ckpt} differs from the one the latest init {newest} runs "
            f"({', '.join(newest_ckpts)}): a different downscaling setup. Say so when "
            "comparing this init with later ones.",
            file=sys.stderr,
        )
    # Target folders: the precipitation products this checkpoint was trained to.
    ckpt_dir = f"{root}/{init}/{model_folder}/{ckpt}"
    targets = ls(ckpt_dir)
    if target is None:
        found = [t for t in _TARGETS if _TARGETS[t] in targets]
        target = found[0] if len(found) == 1 else "era5"
    target_folder = _TARGETS[target]
    if target_folder not in targets:
        raise DataError(
            f"no {target} target under az://{_ACCOUNT}/{ckpt_dir}/ (it has: "
            f"{', '.join(targets) or 'nothing'}). Pass another --target."
        )
    data_dir = f"{ckpt_dir}/{target_folder}/precip_24h/data"
    print(f"Reading az://{_ACCOUNT}/{data_dir}/", file=sys.stderr)

    member_dirs = [d for d in ls(data_dir) if _MEMBER_RE.fullmatch(d)] if members else [None]
    if not member_dirs:
        raise DataError(
            f"no member_* folders under az://{_ACCOUNT}/{data_dir}/ "
            "(this model publishes only one field). Omit --members."
        )

    fields = []
    for member in member_dirs:
        folder = f"{data_dir}/{member}" if member else data_dir
        files = [f for f in ls(folder) if _FILE_RE.fullmatch(f)]
        if not files:
            raise DataError(f"no lead files under az://{_ACCOUNT}/{folder}/ (still publishing?).")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            da = xr.concat(list(pool.map(read, [f"{folder}/{f}" for f in files])), "step")
        # Label each 24 h total by its period start (file lead minus 24 h).
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
    else:
        tp = fields[0]
    # A lead file that is not there stays NaN (never filled); steps end at the last file.
    days = np.arange(int(tp["step"].max() / np.timedelta64(1, "D")) + 1)
    tp = tp.reindex(step=(days * np.timedelta64(1, "D")).astype(tp["step"].dtype))
    tp = tp.sortby("latitude").transpose(..., "step", "latitude", "longitude")
    empty = days[tp.isnull().all([d for d in tp.dims if d != "step"]).values]
    if len(days) < 46 or len(empty):
        gaps = (
            f"; step(s) {', '.join(map(str, empty))} d have no file (left NaN)"
            if len(empty)
            else ""
        )
        print(
            f"WARNING: init {init} has {len(days) - len(empty)} of 46 daily leads{gaps}. "
            "Still publishing? Pass --date with an earlier init for every lead.",
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
        weather_skills_source=(
            f"cumulus-downscaled:{source.strip('/')}/{model_folder}/{ckpt}/{target_folder}"
        ),
        cumulus_downscaled_member_set="members" if members else "ensemble_mean",
    )
    stamp_cf_attrs(ds)
    stamp_precip_amounts(ds)
    ds = to_standard_units(ds)
    ds = precip_amounts_to_rates(ds, interval="1 day", deaccumulate=False)
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()

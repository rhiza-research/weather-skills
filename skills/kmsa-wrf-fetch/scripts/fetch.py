# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "xarray",
#   "zarr>=3",
#   "cftime",
#   "netcdf4",
#   "numpy",
#   "paramiko",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a KMSA WRF forecast netCDF (public GCS or SSH/SFTP) and write a weather-skills standard dataset."""

from __future__ import annotations

import base64
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, require_env
from weather_skills_core.units import (
    convert_dataarray,
    precip_amounts_to_rates,
    stamp_data_interval,
    to_standard_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_HOST_ENV = "KMSA_WRF_SSH_HOST"
_USER_ENV = "KMSA_WRF_SSH_USER"
_PASSWORD_ENV = "KMSA_WRF_SSH_PASSWORD"
_KEY_ENV = "KMSA_WRF_SSH_KEY"
_PORT_ENV = "KMSA_WRF_SSH_PORT"
_HOST_KEY_ENV = "KMSA_WRF_SSH_HOST_KEY"
_DIR_ENV = "KMSA_WRF_REMOTE_DIR"

# Public sample mirror, read anonymously over HTTPS.
_DEFAULT_GCS_PATH = "sheerwater-public-datalake/kmsa-wrf"
_GCS_API = "https://storage.googleapis.com/storage/v1"
_GCS_HTTP = "https://storage.googleapis.com"

# One file per init: YYYYMMDD_to_YYYYMMDD_fcst.nc (first date is the init day).
_FILE_RE = re.compile(r"^(\d{8})_to_(\d{8})_fcst\.nc$")

# -v token -> output variable. tp is rainc + rainnc (both accumulated since init).
_ALIASES = {
    "tp": "tp",
    "precip": "tp",
    "precipitation": "tp",
    "total_precipitation": "tp",
    "rainc": "rainc",
    "rainnc": "rainnc",
    "t2m": "t2m",
    "t2": "t2m",
    "2t": "t2m",
    "temperature_2m": "t2m",
}
_LONG_NAMES = {
    "tp": "Total precipitation",
    "rainc": "Cumulus (convective) precipitation",
    "rainnc": "Grid-scale (non-convective) precipitation",
    "t2m": "2 metre temperature",
}


def _iso(stamp: str) -> str:
    """``YYYYMMDD`` -> ``YYYY-MM-DD``."""
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"


def _connection_settings() -> dict:
    """SSH settings from the env. Never print or log the values."""
    (host,) = require_env(
        _HOST_ENV,
        message=(
            f"{_HOST_ENV} must be set to the KMSA WRF host (host, user@host, or "
            f"user@host:port), with {_PASSWORD_ENV} or {_KEY_ENV} for auth."
        ),
    )
    user = os.environ.get(_USER_ENV) or None
    port = int(os.environ.get(_PORT_ENV) or 22)
    host = host.strip()
    if "@" in host:
        user_part, host = host.rsplit("@", 1)
        user = user or user_part
    if re.fullmatch(r"[^:\[\]]+:\d+", host):
        host, port_part = host.rsplit(":", 1)
        port = int(port_part)
    if not user:
        raise UsageError(f"set {_USER_ENV}, or put the user in {_HOST_ENV} as user@host.")

    password = os.environ.get(_PASSWORD_ENV) or None
    key = os.environ.get(_KEY_ENV) or None
    if not password and not key:
        raise UsageError(f"set {_PASSWORD_ENV} or {_KEY_ENV} (path to a private key file).")
    return {
        "hostname": host,
        "port": port,
        "username": user,
        "password": password,
        "key_filename": os.path.expanduser(key) if key else None,
    }


def _remote_dir(remote_dir: str | None) -> str:
    """The remote folder: --remote-dir, else KMSA_WRF_REMOTE_DIR."""
    value = remote_dir or os.environ.get(_DIR_ENV)
    if not value:
        raise UsageError(f"pass --remote-dir or set {_DIR_ENV} to the remote forecast folder.")
    return value.rstrip("/") or "/"


def _ssh_error(exc: Exception, what: str) -> DataError:
    """Wrap an SSH/SFTP failure as a DataError with an auth hint where it applies."""
    text = f"{type(exc).__name__}: {exc}"
    hint = ""
    if "authentication" in text.lower():
        hint = f" Check {_USER_ENV} / {_PASSWORD_ENV} / {_KEY_ENV}."
    elif "host key" in text.lower() or "BadHostKey" in text:
        hint = f" Check {_HOST_KEY_ENV} against the server's current key."
    return DataError(f"{what} ({text}).{hint}")


@contextmanager
def _sftp():
    """Open an SFTP session with the env credentials.

    The host key is pinned when KMSA_WRF_SSH_HOST_KEY is set (``<type> <base64>``,
    one line of known_hosts without the hostname); otherwise a key already in
    ~/.ssh/known_hosts is checked, and an unknown key is accepted with a warning.
    """
    settings = _connection_settings()
    import paramiko

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    pinned = os.environ.get(_HOST_KEY_ENV)
    if pinned:
        try:
            key_type, key_b64 = pinned.split()[-2:]
            key = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_b64))
        except Exception as exc:  # noqa: BLE001
            raise UsageError(
                f"{_HOST_KEY_ENV} must look like 'ssh-ed25519 AAAA...' ({exc})."
            ) from None
        name = settings["hostname"]
        if settings["port"] != 22:
            name = f"[{name}]:{settings['port']}"
        client.get_host_keys().add(name, key_type, key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
    try:
        client.connect(
            **settings,
            timeout=30,
            look_for_keys=settings["key_filename"] is None and settings["password"] is None,
            allow_agent=False,
        )
        sftp = client.open_sftp()
    except Exception as exc:  # noqa: BLE001
        client.close()
        raise _ssh_error(
            exc, f"SSH connection to {settings['hostname']}:{settings['port']} failed"
        ) from None
    try:
        yield sftp
    finally:
        sftp.close()
        client.close()


def _list_files(sftp, remote_dir: str) -> list[str]:
    """``YYYYMMDD_to_YYYYMMDD_fcst.nc`` names in the remote folder, oldest init first."""
    try:
        names = sftp.listdir(remote_dir)
    except FileNotFoundError:
        raise DataError(f"remote folder {remote_dir} does not exist.") from None
    except Exception as exc:  # noqa: BLE001
        raise _ssh_error(exc, f"listing {remote_dir} failed") from None
    return sorted(name for name in names if _FILE_RE.fullmatch(name))


def _download(sftp, remote_path: str, local_path: Path) -> None:
    try:
        sftp.get(remote_path, str(local_path))
    except Exception as exc:  # noqa: BLE001
        raise _ssh_error(exc, f"download of {remote_path} failed") from None


def _pick_file(names: list[str], date) -> str:
    """The file for init ``date``, or the latest when ``date`` is None.

    The init date is the first ``YYYYMMDD`` in ``YYYYMMDD_to_YYYYMMDD_fcst.nc``.
    """
    if not names:
        raise DataError("no KMSA WRF forecast files (YYYYMMDD_to_YYYYMMDD_fcst.nc) found.")
    if date is None:
        return names[-1]
    inits = {_iso(_FILE_RE.fullmatch(n).group(1)): n for n in names}
    if date.isoformat() not in inits:
        shown = sorted(inits)
        listing = ", ".join(shown if len(shown) <= 10 else ["...", *shown[-10:]])
        raise DataError(
            f"no KMSA WRF forecast for init {date.isoformat()}. Available inits: {listing}."
        )
    return inits[date.isoformat()]


def _to_celsius(da):
    if str(da.attrs.get("units") or "") in {"degree_Celsius", "degC", "celsius"}:
        return da
    converted, _ = convert_dataarray(da, "degree_Celsius")
    converted.attrs["units"] = "degree_Celsius"
    return converted


def _to_standard(raw, names: list[str], file_name: str | None = None):
    """Map a WRF file's (time, lev, lat, lon) onto (step, latitude, longitude) + scalar init."""
    import numpy as np
    import xarray as xr

    missing = {"tp": {"rainnc"}, "rainc": {"rainc"}, "rainnc": {"rainnc"}, "t2m": {"t2"}}
    for name in names:
        absent = missing[name] - set(raw.data_vars)
        if absent:
            raise DataError(
                f"{file_name or 'file'} has no {', '.join(sorted(absent))} for -v {name}. "
                f"Has: {', '.join(raw.data_vars)}"
            )

    ds = raw.squeeze("lev", drop=True) if "lev" in raw.dims else raw
    ds = ds.rename({"lat": "latitude", "lon": "longitude"})
    out = xr.Dataset(coords={k: ds.coords[k] for k in ("time", "latitude", "longitude")})
    for name in names:
        if name == "tp":
            da = ds["rainnc"] + ds["rainc"] if "rainc" in ds else ds["rainnc"]
            da.attrs = {"units": "mm"}
        elif name == "t2m":
            da = ds["t2"].copy()
            da.attrs = {"units": "K"}
        else:
            da = ds[name].copy()
            da.attrs = {"units": "mm"}
        da.attrs["long_name"] = _LONG_NAMES[name]
        out[name] = da.astype("float32")

    times = out["time"].values
    if times.size < 2:
        raise DataError(f"{file_name or 'file'} has {times.size} time step(s); need at least 2.")
    init = times[0]
    if file_name is not None:
        stamp = _FILE_RE.fullmatch(file_name).group(1)
        if np.datetime_as_string(init, unit="D") != _iso(stamp):
            print(
                f"warning: first time {init} does not fall on the file's init date {_iso(stamp)}",
                file=sys.stderr,
            )
    out = out.rename({"time": "step"}).assign_coords(step=times - init, time=init)
    out["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    out["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    out["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    out["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    return out.transpose("step", "latitude", "longitude")


def _standardize(ds):
    """CF attrs, standard units, cumulative precip -> per-step rates."""
    stamp_cf_attrs(ds)
    ds = to_standard_units(ds)
    if "t2m" in ds.data_vars:
        ds["t2m"] = _to_celsius(ds["t2m"])
    ds = precip_amounts_to_rates(ds)
    return stamp_data_interval(ds)


def _resolve_variables(raw: list[str] | None) -> list[str]:
    names = []
    for token in raw or ["tp"]:
        name = _ALIASES.get(token.lower())
        if name is None:
            raise UsageError(
                f"unknown variable {token!r}.\nAvailable: tp (rainc + rainnc), rainc, rainnc, t2m"
            )
        if name not in names:
            names.append(name)
    return names


def _split_gcs_path(gcs_path: str) -> tuple[str, str]:
    """``[gs://]bucket/prefix`` -> (bucket, ``prefix/``)."""
    bucket, _, prefix = gcs_path.removeprefix("gs://").strip("/").partition("/")
    if not bucket:
        raise UsageError(f"--gcs-path {gcs_path!r} must look like bucket/prefix.")
    return bucket, f"{prefix}/" if prefix else ""


def _gcs_list(gcs_path: str) -> list[str]:
    """``*_fcst.nc`` names directly under a public GCS prefix, oldest init first."""
    import json
    import urllib.parse
    import urllib.request

    bucket, prefix = _split_gcs_path(gcs_path)
    names, token = [], None
    while True:
        query = {"prefix": prefix, "delimiter": "/", "fields": "items(name),nextPageToken"}
        if token:
            query["pageToken"] = token
        url = f"{_GCS_API}/b/{bucket}/o?{urllib.parse.urlencode(query)}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                page = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"listing gs://{bucket}/{prefix} failed ({exc}).") from None
        names += [item["name"][len(prefix) :] for item in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    return sorted(name for name in names if _FILE_RE.fullmatch(name))


def _gcs_download(gcs_path: str, file_name: str, local_path: Path) -> None:
    import shutil
    import urllib.parse
    import urllib.request

    bucket, prefix = _split_gcs_path(gcs_path)
    url = f"{_GCS_HTTP}/{bucket}/{urllib.parse.quote(prefix + file_name)}"
    try:
        with urllib.request.urlopen(url, timeout=300) as resp, open(local_path, "wb") as out:
            shutil.copyfileobj(resp, out)
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"download of gs://{bucket}/{prefix}{file_name} failed ({exc}).") from None


def _open_netcdf(path: Path):
    import xarray as xr

    try:
        with xr.open_dataset(path) as ds:
            return ds.load()
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"could not read {path.name} as netCDF ({exc}).") from None


@weather_skill(
    name="kmsa-wrf-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--date")
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help="tp (rainc + rainnc, default), rainc, rainnc, or t2m. Repeatable.",
)
@weather_skill.argument(
    "--source",
    choices=("gcs", "ssh"),
    default="gcs",
    help=(
        "gcs (default): the public bucket at --gcs-path, no credentials. "
        "ssh: the KMSA host over SFTP with KMSA_WRF_SSH_* credentials."
    ),
)
@weather_skill.argument(
    "--gcs-path",
    default=_DEFAULT_GCS_PATH,
    help=f"Public bucket/prefix for --source gcs (default {_DEFAULT_GCS_PATH}).",
)
@weather_skill.argument(
    "--remote-dir",
    default=None,
    help=f"Remote folder for --source ssh (default: ${_DIR_ENV}).",
)
@weather_skill.argument(
    "--input-file",
    default=None,
    help="Convert a local YYYYMMDD_to_YYYYMMDD_fcst.nc instead of fetching.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help="Print the latest init YYYY-MM-DD (or none) on stdout and exit. Downloads nothing.",
)
def fetch(date, bbox, variable, source, gcs_path, remote_dir, input_file, **kwargs):
    """Fetch a KMSA WRF forecast netCDF and write a weather-skills standard dataset.

    Lists ``YYYYMMDD_to_YYYYMMDD_fcst.nc`` in the public GCS prefix (default) or,
    with ``--source ssh``, the remote folder over SFTP with the credentials in
    ``KMSA_WRF_SSH_*``; downloads the requested (or latest) init, deaccumulates
    ``rainc`` + ``rainnc`` into a ``tp`` rate, and returns a Dataset for the
    decorator to write.
    """
    if kwargs.get("probe_latest") is not None:
        if source == "gcs":
            available = _gcs_list(gcs_path)
        else:
            with _sftp() as sftp:
                available = _list_files(sftp, _remote_dir(remote_dir))
        print(_iso(_FILE_RE.fullmatch(available[-1]).group(1)) if available else "none")
        return

    names = _resolve_variables(variable)

    if input_file is not None:
        path = Path(input_file).expanduser()
        if not path.is_file():
            raise UsageError(f"--input-file {path} does not exist.")
        file_name = path.name if _FILE_RE.fullmatch(path.name) else None
        raw = _open_netcdf(path)
        origin = f"kmsa-wrf:{path.name}"
    elif source == "gcs":
        bucket, prefix = _split_gcs_path(gcs_path)
        file_name = _pick_file(_gcs_list(gcs_path), date)
        print(f"Downloading gs://{bucket}/{prefix}{file_name}", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="kmsa-wrf-fetch-") as tmpdir:
            local = Path(tmpdir) / file_name
            _gcs_download(gcs_path, file_name, local)
            raw = _open_netcdf(local)
        origin = f"kmsa-wrf:gs://{bucket}/{prefix}{file_name}"
    else:
        folder = _remote_dir(remote_dir)
        with _sftp() as sftp:
            file_name = _pick_file(_list_files(sftp, folder), date)
            remote_path = f"{folder}/{file_name}"
            print(f"Downloading {remote_path}", file=sys.stderr)
            with tempfile.TemporaryDirectory(prefix="kmsa-wrf-fetch-") as tmpdir:
                local = Path(tmpdir) / file_name
                _download(sftp, remote_path, local)
                raw = _open_netcdf(local)
        origin = f"kmsa-wrf:ssh:{file_name}"

    ds = _to_standard(raw, names, file_name)
    if bbox is not None:
        ds = bbox_subset(ds, list(bbox), lat_dim="latitude", lon_dim="longitude")
    ds.attrs.update(Conventions="CF-1.13", weather_skills_source=origin)
    return _standardize(ds)


if __name__ == "__main__":
    fetch()

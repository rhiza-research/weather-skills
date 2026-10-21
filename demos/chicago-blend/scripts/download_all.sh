#!/bin/bash
# Get every input onto local disk, then check it. Safe to rerun: finished downloads are skipped.
# Needs SKILLS, HERE and ARCHIVE set (see RUNBOOK.md), Google ADC for the GEFS reforecast,
# AZURE_STORAGE_SAS_TOKEN for Cumulus and TAHMO_API_USERNAME / TAHMO_API_PASSWORD for TAHMO.
# The first run takes about 2 hours (TAHMO is the longest); later runs check and finish in seconds.
set -uo pipefail
py() { uv run --project "$SKILLS/.." python -I "$@"; }
pyaz() { uv run --project "$SKILLS/.." --with adlfs --with h5netcdf --with h5py python -I "$@"; }
mkdir -p "$ARCHIVE"

echo "1/4 checking access" >&2
py - <<'PY' || exit 1
import sys
import gcsfs
try:
    gcsfs.GCSFileSystem().info("sheerwater-datalake/caches/gefs_chc_raw/global1_0_precip.zarr/zarr.json")
except Exception as exc:
    sys.exit(f"cannot read gs://sheerwater-datalake ({type(exc).__name__}): run "
             "`gcloud auth application-default login` with an account that can read it")
gcsfs.GCSFileSystem(token="anon").info("weatherbench2/datasets/ifs_extended_range/daily/"
                                       "ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr/.zmetadata")
print("access ok: sheerwater-datalake (private) and weatherbench2 (public)")
PY
pyaz - <<'PY' || exit 1
import os, sys
import adlfs
token = os.environ.get("AZURE_STORAGE_SAS_TOKEN")
if not token:
    sys.exit("AZURE_STORAGE_SAS_TOKEN is not set: it needs a read SAS (sp=rl) for Azure italynorthdata/data")
token = token.split("?", 1)[1] if "?" in token else token
try:
    adlfs.AzureBlobFileSystem(account_name="italynorthdata", sas_token=token).info(
        "data/ml_reforecasts/heartbreaking-portfolio/evaluation_metrics.json")
except Exception as exc:
    sys.exit(f"cannot read the Cumulus hindcast on Azure ({type(exc).__name__}): the SAS may have expired")
print("access ok: Cumulus hindcast (Azure)")
PY
[ -n "${TAHMO_API_USERNAME:-}" ] && [ -n "${TAHMO_API_PASSWORD:-}" ] \
  || { echo "TAHMO_API_USERNAME and TAHMO_API_PASSWORD must be set for the TAHMO download" >&2; exit 1; }
echo "access ok: TAHMO credentials set" >&2

echo "2/4 downloading in parallel; logs in $ARCHIVE: archives_log.txt (ECMWF + GEFS), cumulus_log.txt, chirps_log.txt, tahmo_log.txt" >&2
py "$HERE/0_download_archives.py" "$ARCHIVE" > "$ARCHIVE/archives_log.txt" 2>&1 &
archives=$!
# Two Cumulus hindcasts, one after the other: the full heartbreaking-portfolio run, and the
# gratis-czajkowski run (the one matching the live forecast) for Oct-Dec 2016-2024.
{ pyaz "$HERE/0b_download_cumulus.py" "$ARCHIVE" \
  && CUMULUS_RUN=gratis-czajkowski CUMULUS_VAR=total_precipitation_24h_acc_imerg CUMULUS_MONTHS=10,11,12 \
     CUMULUS_YEARS=2016-2024 pyaz "$HERE/0b_download_cumulus.py" "$ARCHIVE"; } > "$ARCHIVE/cumulus_log.txt" 2>&1 &
cumulus=$!
bash "$HERE/1_chirps.sh" > "$ARCHIVE/chirps_log.txt" 2>&1 &
chirps=$!
bash "$HERE/1b_tahmo.sh" > "$ARCHIVE/tahmo_log.txt" 2>&1 &
tahmo=$!
wait $archives; a=$?
wait $cumulus; u=$?
wait $chirps; c=$?
wait $tahmo; t=$?

echo "3/4 checking the downloads" >&2
fail=0
[ $a -eq 0 ] || { echo "ECMWF/GEFS download failed; see $ARCHIVE/archives_log.txt" >&2; fail=1; }
[ $c -eq 0 ] || { echo "CHIRPS download failed; see $ARCHIVE/chirps_log.txt" >&2; fail=1; }
[ $u -eq 0 ] || { echo "Cumulus download failed; see $ARCHIVE/cumulus_log.txt" >&2; fail=1; }
[ $t -eq 0 ] || { echo "TAHMO download failed; see $ARCHIVE/tahmo_log.txt" >&2; fail=1; }
grep -h FAIL "$ARCHIVE/chirps_log.txt" "$ARCHIVE/tahmo_log.txt" 2>/dev/null >&2 && fail=1
py - "$ARCHIVE" <<'PY' || fail=1
import sys
from pathlib import Path
import numpy as np
import xarray as xr
A = Path(sys.argv[1])
ec = xr.open_zarr(A / "ecmwf_reforecast.zarr")["total_precipitation_24hr"]
ge = xr.open_zarr(A / "gefs_reforecast.zarr", decode_times=False)["precip"]
seasons = sorted(A.glob("chirps/c15_*.zarr"))
cu = xr.open_zarr(A / "cumulus_reforecast.zarr")["precip"] if (A / "cumulus_reforecast.zarr").exists() else None
years = sorted(A.glob("tahmo/tahmo_*.zarr"))
cg = xr.open_zarr(A / "cumulus_gratis-czajkowski.zarr")["precip"] if (A / "cumulus_gratis-czajkowski.zarr").exists() else None
problems = []
if ec.sizes["forecast_time"] != 730 or ec.sizes["hindcast_year"] != 20:
    problems.append(f"ECMWF incomplete: {dict(ec.sizes)}")
if ge.sizes["init_time"] != 1073 or ge.sizes["member"] != 11:
    problems.append(f"GEFS incomplete: {dict(ge.sizes)}")
if cu is None or cu.sizes["time"] != 1301 or cu.sizes["number"] != 10:
    problems.append(f"Cumulus incomplete: {dict(cu.sizes) if cu is not None else 'missing'}")
if cg is None or cg.sizes["time"] != 186 or cg.sizes["number"] != 30:
    problems.append(f"Cumulus gratis-czajkowski incomplete: {dict(cg.sizes) if cg is not None else 'missing'}")
if not years:
    problems.append("TAHMO has no years; rerun 1b_tahmo.sh")
if len(seasons) != 45:
    problems.append(f"CHIRPS has {len(seasons)} of 45 seasons; rerun 1_chirps.sh")
# A spot check that values arrived (Kenya cell, one issuance / init / season).
if not np.isfinite(ec.sel(latitude=0.0, longitude=37.5).isel(forecast_time=0).values).all():
    problems.append("ECMWF values missing at 0N 37.5E")
if not np.isfinite(ge.sel(lat=0.0, lon=37.0).isel(init_time=0).values).all():
    problems.append("GEFS values missing at 0N 37E")
for p in problems:
    print("PROBLEM:", p)
size = sum(f.stat().st_size for f in A.rglob("*") if f.is_file()) / 1e9
span = f"{years[0].stem[-4:]}-{years[-1].stem[-4:]}" if years else "none"
print(f"ECMWF {dict(ec.sizes)}\nGEFS {dict(ge.sizes)}\nCumulus {dict(cu.sizes) if cu is not None else None}")
print(f"Cumulus gratis-czajkowski {dict(cg.sizes) if cg is not None else None}")
print(f"CHIRPS {len(seasons)} seasons\nTAHMO {len(years)} years ({span})\narchive {size:.1f} GB")
sys.exit(1 if problems else 0)
PY

echo "4/4 $([ $fail -eq 0 ] && echo 'all inputs downloaded; next: bash $HERE/run_all.sh' || echo 'NOT complete: fix the problems above and rerun this script')" >&2
exit $fail

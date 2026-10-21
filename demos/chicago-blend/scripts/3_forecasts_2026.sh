#!/bin/bash
# Live forecasts for the prediction: ECMWF 46-day (Thursday init) and GEFS 35-day (the Wednesday before),
# as weekly means on the 1.5° grid. Each init pair is fetched once into $ARCHIVE/live/ and reused;
# delete that folder to fetch it again.
set -euo pipefail
EC_INIT=${EC_INIT:-2026-10-15}; GEFS_INIT=${GEFS_INIT:-2026-10-14}
LAG=$(( ( $(date -j -f %Y-%m-%d $EC_INIT +%s) - $(date -j -f %Y-%m-%d $GEFS_INIT +%s) ) / 86400 ))
CACHE="$ARCHIVE/live/ecmwf_${EC_INIT}_gefs_${GEFS_INIT}"
if [ -d "$CACHE" ]; then
  echo "live forecasts for ECMWF $EC_INIT / GEFS $GEFS_INIT: already downloaded, skipped"
else
  # Built under a temporary name and renamed when complete, so an interrupted fetch is redone.
  TMP="$CACHE.tmp"; rm -rf "$TMP"; mkdir -p "$TMP" && cd "$TMP"
  run() { uv run -q "$@"; }
  run "$SKILLS/dynamical-fetch/scripts/fetch.py" --dataset ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree --date $EC_INIT \
    --bbox 5.25/32.25/-5.25/42.75 --max-lead 35D -v precip -o ecmwf_raw.zarr
  run "$SKILLS/summarize-dim/scripts/summarize_dim.py" -i ecmwf_raw.zarr --dim number --method mean -o ecmwf_em.zarr
  run "$SKILLS/aggregate-temporal/scripts/aggregate.py" -i ecmwf_em.zarr --period weekly --time-dim step -o ecmwf_wk.zarr
  run "$SKILLS/edit-coords/scripts/edit_coords.py" -i ecmwf_wk.zarr --rename precipitation_surface=precip -o ecmwf_wk_p.zarr
  run "$SKILLS/dynamical-fetch/scripts/fetch.py" --dataset noaa-gefs-forecast-35-day --date $GEFS_INIT \
    --bbox 6/31/-6/44 -v precip -o gefs_raw.zarr
  run "$SKILLS/summarize-dim/scripts/summarize_dim.py" -i gefs_raw.zarr --dim number --method mean -o gefs_em.zarr
  run "$SKILLS/aggregate-temporal/scripts/aggregate.py" -i gefs_em.zarr --period daily --time-dim step -o gefs_daily.zarr
  uv run --project "$SKILLS/.." python -I "$HERE/gefs_2026_weekly.py" gefs_daily.zarr gefs_wk_025.zarr $EC_INIT $LAG
  run "$SKILLS/coarsen/scripts/coarsen.py" -i gefs_wk_025.zarr --reference-grid ecmwf_wk.zarr --method conservative -o gefs_wk.zarr
  cd "$ARCHIVE/live" && mv "$TMP" "$CACHE"
fi
mkdir -p "$WORK/f2026"
rm -rf "$WORK/f2026/ecmwf_wk_p.zarr" "$WORK/f2026/gefs_wk.zarr"
cp -R "$CACHE/ecmwf_wk_p.zarr" "$CACHE/gefs_wk.zarr" "$WORK/f2026/"

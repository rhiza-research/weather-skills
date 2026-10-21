#!/bin/bash
# Live Cumulus forecast (run utmost-plane, 30 members, the 1 deg IMERG-trained product that
# matches the gratis-czajkowski hindcast) as weekly means on the 1.5° grid, for the ECMWF init.
# Fetched once into $ARCHIVE/live/cumulus_<init>/ and reused; delete that folder to fetch again.
# Needs AZURE_STORAGE_SAS_TOKEN and step 6 (3_forecasts_2026.sh) for the reference grid.
set -euo pipefail
EC_INIT=${EC_INIT:-2026-10-15}; GEFS_INIT=${GEFS_INIT:-2026-10-14}
CACHE="$ARCHIVE/live/cumulus_${EC_INIT}"
if [ -d "$CACHE" ]; then
  echo "live Cumulus for $EC_INIT: already downloaded, skipped"
else
  # Built under a temporary name and renamed when complete, so an interrupted fetch is redone.
  TMP="$CACHE.tmp"; rm -rf "$TMP"; mkdir -p "$TMP" && cd "$TMP"
  run() { uv run -q "$@"; }
  run "$SKILLS/cumulus-fetch/scripts/fetch.py" --dataset precip_1deg --date $EC_INIT --bbox 6/31/-6/44 -o raw.zarr
  run "$SKILLS/summarize-dim/scripts/summarize_dim.py" -i raw.zarr --dim number --method mean -o em.zarr
  run "$SKILLS/aggregate-temporal/scripts/aggregate.py" -i em.zarr --period weekly --time-dim step -o wk_1deg.zarr
  run "$SKILLS/coarsen/scripts/coarsen.py" -i wk_1deg.zarr \
    --reference-grid "$ARCHIVE/live/ecmwf_${EC_INIT}_gefs_${GEFS_INIT}/ecmwf_wk_p.zarr" --method conservative -o wk.zarr
  cd "$ARCHIVE/live" && mv "$TMP" "$CACHE"
fi
mkdir -p "$WORK/f2026"
rm -rf "$WORK/f2026/cumulus_wk.zarr"
cp -R "$CACHE/wk.zarr" "$WORK/f2026/cumulus_wk.zarr"

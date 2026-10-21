#!/bin/bash
# Every live init since LIVE_FROM (default 2026-09-01), with ensemble members, into $ARCHIVE/live_inits/:
#   ecmwf/ecmwf_YYYY-MM-DD.zarr     ECMWF 46-day, 101 members, daily, 1.5 deg, East Africa
#   cumulus/cumulus_YYYY-MM-DD.zarr Cumulus live (utmost-plane, 1 deg IMERG-trained), 30 members, East Africa
#   gefs/gefs_YYYY-MM-DD.zarr       GEFS 35-day, 31 members, aggregated to daily, 0.25 deg, Ethiopia + Kenya + Uganda
# Each file is written under a tmp_ name and renamed when complete, so an existing init is never
# fetched again. Needs AZURE_STORAGE_SAS_TOKEN for Cumulus. MODELS (default "ecmwf cumulus gefs")
# picks which to fetch; run one model per process to fetch them in parallel.
set -uo pipefail
FROM=${LIVE_FROM:-2026-09-01}
EA=17.998/21.888/-26.742/51.134
# Ethiopia + Kenya + Uganda (resolve-region ETH, KEN, UGA), widened by 1 deg for regridding.
GEFS_BOX=${GEFS_BOX:-15.85/28.57/-5.70/48.96}
mkdir -p "$ARCHIVE/live_inits"
for model in ${MODELS:-ecmwf cumulus gefs}; do
  dir="$ARCHIVE/live_inits/$model"; mkdir -p "$dir" && cd "$dir"
  d=$FROM; today=$(date +%Y-%m-%d); n=0; skipped=0
  while [[ "$d" < "$today" ]] || [[ "$d" == "$today" ]]; do
    out=${model}_$d.zarr
    if [ -d "$out" ]; then skipped=$((skipped + 1)); d=$(date -j -v+1d -f %Y-%m-%d "$d" +%Y-%m-%d); continue; fi
    rm -rf "tmp_$out" "raw_$out"
    case $model in
      ecmwf)   uv run -q "$SKILLS/dynamical-fetch/scripts/fetch.py" --dataset ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree \
                 --date $d --bbox $EA -v precip -o "tmp_$out" ;;
      cumulus) uv run -q "$SKILLS/cumulus-fetch/scripts/fetch.py" --dataset precip_1deg --date $d --bbox $EA -o "tmp_$out" ;;
      gefs)    uv run -q "$SKILLS/dynamical-fetch/scripts/fetch.py" --dataset noaa-gefs-forecast-35-day --date $d \
                 --bbox $GEFS_BOX -v precip -o "raw_$out" \
               && uv run -q "$SKILLS/aggregate-temporal/scripts/aggregate.py" -i "raw_$out" --period daily --time-dim step \
                 -o "tmp_$out" && rm -rf "raw_$out" ;;
    esac > "tmp_$out.log" 2>&1 && mv "tmp_$out" "$out" && rm -f "tmp_$out.log" && n=$((n + 1)) \
      || { echo "$model $d: not available ($(grep -m1 -E 'Error|error' "tmp_$out.log" | cut -c1-120))"; rm -rf "tmp_$out" "raw_$out" "tmp_$out.log"; }
    d=$(date -j -v+1d -f %Y-%m-%d "$d" +%Y-%m-%d)
  done
  echo "$model: $n new inits, $skipped already downloaded"
done

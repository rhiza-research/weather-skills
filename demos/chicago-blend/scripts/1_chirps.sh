#!/bin/bash
# One-time download: CHIRPS final, 1 Oct - 31 Jan of each season 1981-2025, regridded
# to 1.5° cells, into $ARCHIVE/chirps (seasons already there are skipped).
# CHIRPS_BOX (N/W/S/E) defaults to the Kenya analysis box; it must cover whole 1.5°
# cells, i.e. reach 0.75° past the outermost cell centres.
set -uo pipefail
BOX=${CHIRPS_BOX:-5.25/32.25/-5.25/42.75}
mkdir -p "$ARCHIVE/chirps" && cd "$ARCHIVE/chirps"
skipped=0
for y in $(seq 1981 2025); do
  # A season appears under its final name only once complete, so an existing one is never fetched again.
  [ -d c15_$y.zarr ] && { skipped=$((skipped + 1)); continue; }
  rm -rf c05_$y.zarr tmp_c15_$y.zarr
  uv run -q "$SKILLS/dynamical-fetch/scripts/fetch.py" --dataset ucsb-chc-chirps-analysis-final \
    --start-time $y-10-01 --end-time $((y+1))-01-31 --bbox $BOX -v precip -o c05_$y.zarr >/dev/null 2>&1 \
  && uv run -q "$SKILLS/coarsen/scripts/coarsen.py" -i c05_$y.zarr --target-resolution 1.5 --offset 0 \
    --method conservative -o tmp_c15_$y.zarr >/dev/null 2>&1 \
  && mv tmp_c15_$y.zarr c15_$y.zarr && rm -rf c05_$y.zarr && echo "ok $y" || echo "FAIL $y"
done
echo "chirps: $skipped of 45 seasons already downloaded, skipped"

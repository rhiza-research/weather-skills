#!/bin/bash
# Whole analysis from local data, timed per step.
# Needs: SKILLS, PLOT, HERE, WORK, OUT, ARCHIVE set, and the one-time downloads in $ARCHIVE:
# ecmwf_reforecast.zarr + gefs_reforecast.zarr (0_download_archives.py) and chirps/c15_YYYY.zarr
# (1_chirps.sh). Only step 6 (the live 2026 forecasts) uses the network.
set -euo pipefail
py() { uv run --project "$SKILLS/.." python -I "$@"; }
t0=$(date +%s); last=$t0
lap() { now=$(date +%s); printf '%-28s %4ds  (total %ds)\n' "$1" $((now - last)) $((now - t0)) >&2; last=$now; }
ls -d "$ARCHIVE"/ecmwf_reforecast.zarr "$ARCHIVE"/gefs_reforecast.zarr >/dev/null
[ "$(ls -d "$ARCHIVE"/chirps/c15_*.zarr | wc -l)" -eq 45 ] || { echo "need 45 CHIRPS seasons in $ARCHIVE/chirps; run 1_chirps.sh" >&2; exit 1; }
# The season files are small (about 3 MB in all); events are written next to them in $WORK.
mkdir -p "$WORK/chirps" && cp -R "$ARCHIVE"/chirps/c15_*.zarr "$WORK/chirps/"
py "$HERE/prep_hindcasts.py" "$WORK" >/dev/null 2>&1
uv run -q "$SKILLS/coarsen/scripts/coarsen.py" -i "$WORK/gefs_hind_1deg.zarr" --reference-grid "$WORK/ecmwf_hind.zarr" \
  --method conservative -o "$WORK/gefs_hind.zarr" >/dev/null 2>&1
lap "2  hindcasts (local)"
py "$HERE/events.py" "$WORK" >/dev/null 2>&1;            lap "3  onset events"
py "$HERE/clim.py" "$WORK" >/dev/null 2>&1;              lap "4  climatology"
py "$HERE/cv.py" "$WORK" > "$OUT/cv_brier_scores.txt" 2>/dev/null
cp "$WORK/cv_predictions.csv" "$OUT/";                   lap "5  cross-validation"
bash "$HERE/3_forecasts_2026.sh" >/dev/null 2>&1;        lap "6  live 2026 forecasts"
bash "$HERE/4_fit_blend.sh" >/dev/null 2>&1;             lap "7  final fits + blends"
bash "$HERE/5_plot.sh" >/dev/null 2>&1;                  lap "8  maps"
grep -A13 "^ *cfg" "$OUT/cv_brier_scores.txt" >&2 || true

#!/bin/bash
# The member blend for OND onset, end to end from the local archive: observed onsets, member
# predictors, cross-validated skill, fitted weights, the 2026 blended forecast, maps and a report.
#
# Event: the ICPAC rainy-season onset falls in the lead week (first onset since 15 Sep), weeks 1-2,
# on the 1.5 deg cells south of 8 N. Predictors: the share of each model's members whose first onset
# falls in the week (ECMWF 2022-cycle reforecast 2002-2021, Cumulus hindcast 2016-2021).
#
# Needs SKILLS, PLOT, HERE and OUT set. ARCHIVE (default $HOME/chicago-demo/gates-demo/archive, a copy
# of gs://sheerwater-datalake/gates-demo/archive), RUN (default $HOME/chicago-demo/run) and INIT (the
# live init, default 2026-10-15) may be set. RUN is rebuilt every time.
set -euo pipefail
ARCHIVE=${ARCHIVE:-$HOME/chicago-demo/gates-demo/archive}
RUN=${RUN:-$HOME/chicago-demo/run}
INIT=${INIT:-2026-10-15}
py() { uv run --project "$SKILLS/.." python -I "$@"; }
t0=$(date +%s); last=$t0
lap() { now=$(date +%s); printf '%-34s %4ds  (total %ds)\n' "$1" $((now - last)) $((now - t0)) >&2; last=$now; }

rm -rf "$RUN"; mkdir -p "$RUN" "$OUT"
echo "$INIT" > "$RUN/init.txt"
py "$HERE/members_events.py" "$ARCHIVE" "$RUN" >/dev/null 2>&1
lap "observed onsets (CHIRPS)"
py "$HERE/members_predictors.py" "$ARCHIVE" "$RUN" 2022 cumulus >/dev/null 2>&1
py "$HERE/members_live.py" "$ARCHIVE" "$RUN" "$INIT" ecmwf cumulus >/dev/null 2>&1
lap "member predictors (hindcast + live)"
EC_INIT=$INIT py "$HERE/clim.py" "$RUN" >/dev/null 2>&1
py "$HERE/cv.py" "$RUN" cumulus-no-gefs > "$RUN/cv.txt" 2>/dev/null
lap "cross-validation"
py "$HERE/fit_blend.py" "$RUN" ecmwf cumulus ecmwf+cumulus >/dev/null 2>&1
lap "fit weights + blend 2026"
for pair in "ecmwf ECMWF" "cumulus Cumulus" "ecmwf+cumulus ECMWF + Cumulus"; do
  set -- $pair; cfg=$1; shift; name="$*"
  uv run -q "$PLOT" -i "$RUN/p2026_$cfg.zarr" -o "$OUT/map_$cfg.png" --spec "{\"layout\": {\"title\": {\"text\": \"$name blend: chance the rainy season starts in the week\", \"subtitle\": {\"text\": \"ICPAC onset, first since 15 Sep. Init $INIT\"}}}, \"data\": [{\"uid\": \"0\", \"zmin\": 0, \"zmax\": 0.6}]}" >/dev/null 2>&1
done
for country in Kenya Uganda; do
  # The country's cells (clip-region keeps every 1.5 deg cell that touches it), for the report.
  py "$SKILLS/clip-region/scripts/clip.py" -i "$RUN/grid.zarr" --region $country -o "$RUN/cells_$country.zarr" >/dev/null 2>&1
  py "$SKILLS/clip-region/scripts/clip.py" -i "$RUN/p2026_ecmwf+cumulus.zarr" --region $country -o "$RUN/p2026_ecmwf+cumulus_$country.zarr" >/dev/null 2>&1
  uv run -q "$PLOT" -i "$RUN/p2026_ecmwf+cumulus_$country.zarr" -o "$OUT/map_ecmwf+cumulus_$country.png" --spec "{\"layout\": {\"title\": {\"text\": \"$country, ECMWF + Cumulus blend: chance the rainy season starts in the week\", \"subtitle\": {\"text\": \"ICPAC onset, first since 15 Sep. Init $INIT\"}}}, \"data\": [{\"uid\": \"0\", \"zmin\": 0, \"zmax\": 0.6}]}" >/dev/null 2>&1
done
py "$HERE/report.py" "$OUT/report.md" "$RUN" >/dev/null
lap "maps + report"
echo "report: $OUT/report.md" >&2

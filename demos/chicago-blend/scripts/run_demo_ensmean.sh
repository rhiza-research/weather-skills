#!/bin/bash
# The Chicago-method blend, end to end: data, observed events, cross-validated skill, fitted
# weights, the 2026 blended forecast, maps and a report, for two events (rainy-season onset and
# a wet week) to show the blend works for any event. About 2 minutes once the skills are installed.
#
# Needs SKILLS, PLOT, HERE and OUT set, and GOOGLE_APPLICATION_CREDENTIALS (the demo service-account
# key) for the first download. DATA (default $HOME/chicago-demo/demo-pack) and RUN (default
# $HOME/chicago-demo/run) may be set. Rerunning skips the download; RUN is rebuilt every time.
set -euo pipefail
PACK=${PACK:-gs://sheerwater-datalake/gates-demo/demo-pack}
DATA=${DATA:-$HOME/chicago-demo/demo-pack}
RUN=${RUN:-$HOME/chicago-demo/run}
py() { uv run --project "$SKILLS/.." python -I "$@"; }
t0=$(date +%s); last=$t0
lap() { now=$(date +%s); printf '%-34s %4ds  (total %ds)\n' "$1" $((now - last)) $((now - t0)) >&2; last=$now; }

mkdir -p "$(dirname "$DATA")" "$OUT"
py "$HERE/download_pack.py" "$PACK" "$DATA" >&2
INIT=$(cat "$DATA/init.txt")
lap "data (demo pack)"

for EVENT in onset wetweek; do
  case $EVENT in
    onset)   export EVENT_RULE=icpac-onset EVENT_FIRST_IN=7d
             LABEL="ICPAC rainy-season onset falls in the lead week (first onset since 1 October)" ;;
    wetweek) export EVENT_RULE="precip sum 7d >= 25" EVENT_FIRST_IN=
             LABEL="Wet week: at least 25 mm of rain in the lead week" ;;
  esac
  R=$RUN/$EVENT; rm -rf "$R"
  for sub in main c2022; do
    mkdir -p "$R/$sub/chirps"
    cp -R "$DATA"/chirps/c15_*.zarr "$R/$sub/chirps/"
    cp -R "$DATA/$sub/"*.zarr "$DATA/live" "$R/$sub/"
    echo "$LABEL" > "$R/$sub/event.txt"; echo "$INIT" > "$R/$sub/init.txt"
  done
  py "$HERE/events.py" "$R/main" >/dev/null 2>&1
  cp -R "$R/main/obs_event.zarr" "$R/c2022/" && cp -R "$R/main/chirps/"ev_*.zarr "$R/c2022/chirps/"
  lap "$EVENT: observed events (indicator)"
  EC_INIT=$INIT py "$HERE/clim.py" "$R/main" >/dev/null 2>&1
  EC_INIT=$INIT py "$HERE/clim.py" "$R/c2022" >/dev/null 2>&1
  py "$HERE/cv.py" "$R/main" > "$R/main/cv.txt" 2>/dev/null
  py "$HERE/cv.py" "$R/c2022" cumulus-no-gefs > "$R/c2022/cv.txt" 2>/dev/null
  lap "$EVENT: cross-validation"
  py "$HERE/fit_blend.py" "$R/main" ecmwf+gefs >/dev/null 2>&1
  py "$HERE/fit_blend.py" "$R/c2022" cumulus ecmwf+cumulus >/dev/null 2>&1
  lap "$EVENT: fit weights + blend 2026"
  mkdir -p "$OUT/$EVENT"
  for pair in "main ecmwf+gefs ECMWF + GEFS" "c2022 cumulus Cumulus"; do
    set -- $pair; sub=$1; cfg=$2; shift 2; name="$*"
    # The ECMWF + GEFS blend keeps a length-one init (time) dim; the map needs it picked.
    pick=""; [ $sub = main ] && pick=', "meta": {"source": {"isel": {"time": 0}}}'
    uv run -q "$PLOT" -i "$R/$sub/p2026_$cfg.zarr" -o "$OUT/$EVENT/map_$cfg.png" --spec "{\"layout\": {\"title\": {\"text\": \"$name blend: probability of the event, OND 2026\", \"subtitle\": {\"text\": \"$LABEL. Init $INIT\"}}}, \"data\": [{\"uid\": \"0\", \"zmin\": 0, \"zmax\": 0.6$pick}]}" >/dev/null 2>&1
  done
  py "$HERE/report.py" "$OUT/$EVENT/report.md" "Blended forecast: $LABEL" "$R" >/dev/null
  lap "$EVENT: maps + report"
done
echo "reports: $OUT/onset/report.md and $OUT/wetweek/report.md" >&2

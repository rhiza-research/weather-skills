#!/bin/bash
# The member blend for OND onset, end to end from the local archive: observed onsets, member
# predictors, cross-validated skill, fitted weights, the 2026 blended forecast, maps and a report.
#
# Event: the season's first ICPAC rainy-season onset (since 15 Sep) falls within 7 / 14 days of the
# init, on the 1.5 deg cells south of 8 N: the chance the rains have started by then, if they had not
# by the init. Predictors: the share of each model's members whose first onset falls within the window
# (ECMWF 2022-cycle reforecast 2002-2021, Cumulus hindcast 2016-2021). Cells where CHIRPS already
# shows an onset before the init are set to 1: the rains have started.
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
py "$HERE/started.py" "$ARCHIVE" "$RUN" "$INIT" p2026_ecmwf p2026_cumulus p2026_ecmwf+cumulus \
  clim_2026_analog clim_2026_all >/dev/null 2>&1
lap "fit weights + blend 2026"
# Panel titles: the last day of each window from the init (7 and 14 days).
BY7=$(py -c "import pandas as pd; print((pd.Timestamp('$INIT') + pd.Timedelta(days=6)).strftime('%-d %b'))")
BY14=$(py -c "import pandas as pd; print((pd.Timestamp('$INIT') + pd.Timedelta(days=13)).strftime('%-d %b'))")
spec() {  # $1: map title
  echo "{\"layout\": {\"title\": {\"text\": \"$1\", \"subtitle\": {\"text\": \"ICPAC onset, first since 15 Sep. Init $INIT. Already started by the init (CHIRPS): 1\"}}, \"annotations\": [{\"name\": \"panel-title-1\", \"text\": \"By $BY7\"}, {\"name\": \"panel-title-2\", \"text\": \"By $BY14\"}]}, \"data\": [{\"uid\": \"BLEND\", \"zmin\": 0, \"zmax\": 1, \"meta\": {\"facet\": \"window\", \"source\": {\"isel\": {\"step\": 0}}}}]}"
}
for pair in "ecmwf ECMWF" "cumulus Cumulus" "ecmwf+cumulus ECMWF + Cumulus"; do
  set -- $pair; cfg=$1; shift; name="$*"
  uv run -q "$PLOT" --layer "heatmap:$RUN/p2026_$cfg.zarr=BLEND" -o "$OUT/map_$cfg.png" --spec "$(spec "$name blend: chance the rainy season has started")" >/dev/null 2>&1
done
for country in Kenya Uganda; do
  # The country's cells (clip-region keeps every 1.5 deg cell that touches it), for the report.
  py "$SKILLS/clip-region/scripts/clip.py" -i "$RUN/grid.zarr" --region $country -o "$RUN/cells_$country.zarr" >/dev/null 2>&1
  py "$SKILLS/clip-region/scripts/clip.py" -i "$RUN/p2026_ecmwf+cumulus.zarr" --region $country -o "$RUN/p2026_ecmwf+cumulus_$country.zarr" >/dev/null 2>&1
  uv run -q "$PLOT" --layer "heatmap:$RUN/p2026_ecmwf+cumulus_$country.zarr=BLEND" -o "$OUT/map_ecmwf+cumulus_$country.png" --spec "$(spec "$country, ECMWF + Cumulus blend: chance the rainy season has started")" >/dev/null 2>&1
done
py "$HERE/report.py" "$OUT/report.md" "$RUN" >/dev/null
lap "maps + report"
echo "report: $OUT/report.md" >&2

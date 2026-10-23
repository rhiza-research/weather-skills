#!/bin/bash
# Onset blend (mme-fit logistic) from Sheerwater's tiled hindcast stores, for any country, weather-skills only.
#
# Usage: COUNTRY=Kenya MODELS="ec cu" bash run.sh
#   COUNTRY       country name or ISO3 code (resolve-region / clip-region). Default Kenya.
#   MODELS        any of ec (ECMWF), cu (Cumulus), ge (GEFS). Default "ec cu". The seasons are those all
#                 the chosen models have hindcasts for: ec+cu 2016-2021, ec+ge 1999-2018, ec+cu+ge 2016-2018.
#   SEASON_START  MM-DD the onset count restarts each year. Default 09-15 (East African short rains).
#   INIT_DAYS     inits from SEASON_START to this many days later. Default 107 (to 31 Dec).
#   EVENT         onset (default): the season's first RULE day falls within WINDOW days of the init.
#                 rule: RULE itself, true or false on each date; give it its own window, e.g. a dry spell
#                 starting within 7 days: RULE="precip consecutive-below 1 7d within 7d".
#   RULE          indicator rule. Default icpac-onset (chc-onset is the other onset alias).
#   WINDOW        EVENT=onset: days from the init within which the onset must fall, 7 or 14. Default 7
#                 (GEFS: 7 only).
#   FIRST, LAST   limit the seasons (years the seasons start in). Default: every season available.
#   WORK          working files, emptied every run. Default ./work.
#   CACHE         fetched hindcasts kept between runs, one store per season, model and box. Default ./cache.
# Event: the season's first onset since SEASON_START falls within WINDOW days of the init (EVENT=onset),
# or RULE is true at the init (EVENT=rule). Predictor: each model's share of members for which it is.
# Skill: leave one season out; the held-out seasons scored together (Brier skill vs the base rate),
# on the 1.5 deg cells that touch the country.
set -euo pipefail
SK=$(cd "$(dirname "$0")/../../../skills" && pwd)
COUNTRY=${COUNTRY:-Kenya}; MODELS=${MODELS:-ec cu}; SEASON_START=${SEASON_START:-09-15}
INIT_DAYS=${INIT_DAYS:-107}; RULE=${RULE:-icpac-onset}; WINDOW=${WINDOW:-7}; EVENT=${EVENT:-onset}
# The event's indicator options: the first RULE day since the season start within WINDOW days, or RULE as is.
case $EVENT in onset) fc_event=(--first-in ${WINDOW}d --season-start $SEASON_START); obs_event=(--first-in ${WINDOW}d);;
  rule) fc_event=(); obs_event=();; *) echo "EVENT must be onset or rule" >&2; exit 2;; esac
W=${WORK:-$(dirname "$0")/work}; rm -rf "$W"; mkdir -p "$W"
CACHE=${CACHE:-$(dirname "$0")/cache}; mkdir -p "$CACHE"
sk() { local s=$1; shift; uv run -q "$SK/$s" "$@"; }
py() { uv run -q --with xarray --with zarr python -c "$@"; }
case " $MODELS " in *" ge "*) [ "$EVENT" = rule ] || [ "$WINDOW" = 7 ] || { echo "GEFS runs 35 days: WINDOW must be 7" >&2; exit 2; };; esac

# The country's box, as the 1.5 deg ECMWF cell centres that cover it, a box 1.5 deg wider for the
# 1 deg models, and the cells' edges for CHIRPS.
read -r EC_BOX WIDE_BOX CH_BOX <<< "$(py "
import math
n, w, s, e = map(float, '$(sk resolve-region/scripts/resolve.py "$COUNTRY")'.split('/'))
c = [math.ceil(n / 1.5) * 1.5, math.floor(w / 1.5) * 1.5, math.floor(s / 1.5) * 1.5, math.ceil(e / 1.5) * 1.5]
box = lambda pad: '/'.join(f'{v:g}' for v in (c[0] + pad, c[1] - pad, c[2] - pad, c[3] + pad))
print(box(0), box(1.5), box(0.75))")"
# Seasons every chosen model has hindcasts in (first and last init of each store).
YEARS=$(py "
import datetime as d
span = {'ec': ('1996-01-04', '2021-12-29'), 'ge': ('1998-12-30', '2019-08-28'), 'cu': ('2016-01-03', '2024-12-31')}
mm, dd = map(int, '$SEASON_START'.split('-'))
ok = lambda y, m: str(d.date(y, mm, dd)) <= span[m][1] and str(d.date(y, mm, dd) + d.timedelta($INIT_DAYS)) >= span[m][0]
print(' '.join(str(y) for y in range(${FIRST:-1996}, ${LAST:-2024} + 1) if all(ok(y, m) for m in '$MODELS'.split())))")
echo "$COUNTRY: cells $EC_BOX, seasons $YEARS, models $MODELS, event $EVENT: $RULE${obs_event:+ within $WINDOW days} from $SEASON_START"

# Hindcasts, from CACHE when the same fetch ran before (renamed in only once it succeeds).
hindcast() {  # name in WORK, then hindcast-fetch arguments
  local name=$1; shift
  local key; key=$(echo "$*" | sed 's/--//g; s/[ /]/_/g')
  if [ ! -d "$CACHE/$key.zarr" ]; then
    rm -rf "$CACHE/$key.partial.zarr"
    sk hindcast-fetch/scripts/fetch.py "$@" -o "$CACHE/$key.partial.zarr"
    mv "$CACHE/$key.partial.zarr" "$CACHE/$key.zarr"
  fi
  ln -s "$CACHE/$key.zarr" "$W/$name.zarr"
}
dataset() { case $1 in ec) echo "ecmwf --members --bbox $EC_BOX";; cu) echo "cumulus --bbox $WIDE_BOX";; ge) echo "gefs --bbox $WIDE_BOX";; esac; }

for y in $YEARS; do
  read -r start end obs_end <<< "$(py "
import datetime as d
s = d.date($y, *map(int, '$SEASON_START'.split('-')))
print(s, s + d.timedelta($INIT_DAYS), s + d.timedelta($INIT_DAYS + 32))")"
  for m in $MODELS; do hindcast "${m}_${y}_raw" --dataset $(dataset $m) --start-time $start --end-time $end; done
  # Inits every chosen model ran this season (mme-fit pairs identical init dates).
  dates=$(py "
import xarray as xr
inits = [set(xr.open_zarr('$W/' + m + '_${y}_raw.zarr')['time'].values.astype('datetime64[D]').astype(str)) for m in '$MODELS'.split()]
print(' '.join(f'--value {t}' for t in sorted(set.intersection(*inits))))")
  [ -n "$dates" ] || { echo "$y: no shared inits, season skipped"; continue; }
  echo "$y: $(echo $dates | wc -w | awk '{print $1 / 2}') shared inits"; kept="${kept:-} $y"
  for m in $MODELS; do
    sk select/scripts/select_dim.py -i "$W/${m}_${y}_raw.zarr" --dim time $dates -o "$W/${m}_$y.zarr"
    sk indicator/scripts/indicator.py -i "$W/${m}_$y.zarr" --rule "$RULE" ${fc_event[@]+"${fc_event[@]}"} --probability --time-dim step -v precip -o "$W/${m}_${y}_share.zarr"
    sk select/scripts/select_dim.py -i "$W/${m}_${y}_share.zarr" --dim step --index 0 -o "$W/${m}_${y}_step0.zarr"
    if [ $m = ec ]; then mv "$W/${m}_${y}_step0.zarr" "$W/p_${m}_$y.zarr"
    else sk coarsen/scripts/coarsen.py -i "$W/${m}_${y}_step0.zarr" --reference-grid "$W/ec_${y}_raw.zarr" --method conservative -o "$W/p_${m}_$y.zarr"; fi
  done
  # The observed event this season: CHIRPS from the season start on the 1.5 deg cells.
  sk chirps-fetch/scripts/fetch.py --start-time $start --end-time $obs_end --bbox $CH_BOX -o "$W/chirps_$y.zarr"
  sk coarsen/scripts/coarsen.py -i "$W/chirps_$y.zarr" --reference-grid "$W/ec_${y}_raw.zarr" --method conservative -o "$W/chirps15_$y.zarr"
  sk indicator/scripts/indicator.py -i "$W/chirps15_$y.zarr" --rule "$RULE" ${obs_event[@]+"${obs_event[@]}"} -v precip -o "$W/obs_$y.zarr"
done
YEARS=${kept:-}
# Score only the cells that touch the country: the obs elsewhere are left out of every fit and score.
sk concat/scripts/concat.py $(for y in $YEARS; do echo -i "$W/obs_$y.zarr"; done) --dim time -o "$W/obs_box.zarr"
sk clip-region/scripts/clip.py -i "$W/obs_box.zarr" --region "$COUNTRY" -o "$W/obs.zarr"

# Leave one season out: fit on the others, blend the held-out one; score all held-out seasons together.
sets="$MODELS"; [ "$(echo $MODELS | wc -w)" -gt 1 ] && sets="$MODELS ${MODELS// /+}"
for set in $sets; do
  names=$(echo "${set//+/ }" | sed 's/ec/ecmwf/; s/cu/cumulus/; s/ge/gefs/; s/ /,/g')
  held=()
  for test in $YEARS; do
    train=(); blend=()
    for m in ${set//+/ }; do
      sk concat/scripts/concat.py $(for y in $YEARS; do [ $y = $test ] || echo -i "$W/p_${m}_$y.zarr"; done) --dim time -o "$W/train_$m.zarr"
      train+=(--forecast "$W/train_$m.zarr"); blend+=(--forecast "$W/p_${m}_$test.zarr")
    done
    sk mme-fit/scripts/mme_fit.py "${train[@]}" --names "$names" -v probability --obs "$W/obs.zarr" --model logistic -o "$W/w.zarr"
    sk mme-blend/scripts/mme_blend.py "${blend[@]}" --names "$names" -v probability --weights "$W/w.zarr" -o "$W/blend_$test.zarr"
    held+=(-i "$W/blend_$test.zarr")
  done
  sk concat/scripts/concat.py "${held[@]}" --dim time -o "$W/heldout.zarr"
  echo "$names: $(sk verify/scripts/verify.py --forecast "$W/heldout.zarr" --obs "$W/obs.zarr" --metric brier --probability -o "$W/brier.zarr" | grep verify)"
done

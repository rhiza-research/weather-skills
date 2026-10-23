#!/bin/bash
# Cirrus onset blend (mme-fit logistic) from Sheerwater's tiled hindcast stores, weather-skills only.
#
# Usage: WORK=/path/to/scratch bash run.sh      (WORK defaults to ./work beside this script)
#
# Event: the season's first ICPAC onset (since 15 Sep) falls within W days of the init (W = 7, 14).
# Predictors: per model, the share of its members whose first onset since 15 Sep falls within W days
# of the init (indicator --first-in Wd --season-start 09-15 --probability, read at step 0).
# Inits: 15 Sep - 31 Dec 2016-2018 on the dates all three models ran. Grid: the ECMWF 1.5 deg cells
# 6S-7.5N, 28.5-48E. Skill: leave one season out (fit on two, blend the third), pooled Brier skill
# score vs the observed base rate, on each set's own samples and then on the samples every model
# shares. Finished steps are skipped on a rerun; delete WORK to start over.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SK=$(cd "$HERE/../../../skills" && pwd)   # this checkout's skills
W=${WORK:-$HERE/work}; mkdir -p "$W"
SEASONS="2016 2017 2018"
EC_BOX=7.5/28.5/-6/48          # 1.5 deg centres of the target cells
WIDE_BOX=9/27/-8/50            # 1 deg models: wider, so every 1.5 deg cell is fully covered
CH_BOX=8.25/27.75/-6.75/48.75  # CHIRPS: the target cells' edges
sk() { local s=$1; shift; uv run -q "$SK/$s" "$@"; }
t0=$(date +%s); lap() { echo "[$(( $(date +%s) - t0 ))s] $*"; }

# ── 1. Hindcasts: one read per model (every store chunk holds all inits) ──
[ -d "$W/ec_raw.zarr" ] || sk hindcast-fetch/scripts/fetch.py --dataset ecmwf --members --start-time 2016-09-15 --end-time 2018-12-31 --bbox $EC_BOX -o "$W/ec_raw.zarr"
[ -d "$W/cu_raw.zarr" ] || sk hindcast-fetch/scripts/fetch.py --dataset cumulus --start-time 2016-09-15 --end-time 2018-12-31 --bbox $WIDE_BOX -o "$W/cu_raw.zarr"
[ -d "$W/ge_raw.zarr" ] || sk hindcast-fetch/scripts/fetch.py --dataset gefs --start-time 2016-09-15 --end-time 2018-12-31 --bbox $WIDE_BOX -o "$W/ge_raw.zarr"
lap "hindcasts fetched"

for y in $SEASONS; do
  # Shared inits, 15 Sep - 31 Dec: the init labels all three fetched hindcasts carry (labels only).
  DATES=$(uv run -q --with xarray --with zarr python -c "
import xarray as xr
sets = [set(xr.open_zarr('$W/' + m + '_raw.zarr')['time'].values.astype('datetime64[D]').astype(str)) for m in ('ec', 'cu', 'ge')]
print(' '.join(t for t in sorted(set.intersection(*sets)) if '$y-09-15' <= t <= '$y-12-31'))")
  VALS=$(for x in $DATES; do printf -- "--value %s " "$x"; done)
  echo "$y inits: $DATES"
  for m in ec cu ge; do
    [ -d "$W/${m}_$y.zarr" ] || sk select/scripts/select_dim.py -i "$W/${m}_raw.zarr" --dim time $VALS -o "$W/${m}_$y.zarr"
    for w in 7 14; do
      [ "$m" = ge ] && [ $w = 14 ] && continue   # GEFS runs 35 days: decides onset only within 7
      out=$W/p${w}_${m}_$y.zarr; [ -d "$out" ] && continue
      sk indicator/scripts/indicator.py -i "$W/${m}_$y.zarr" --rule icpac-onset --first-in ${w}d \
        --season-start 09-15 --probability --time-dim step -v precip -o "$W/tmp_by_day.zarr"
      sk select/scripts/select_dim.py -i "$W/tmp_by_day.zarr" --dim step --index 0 -o "$W/tmp_step0.zarr"
      if [ "$m" = ec ]; then
        rm -rf "$out"; mv "$W/tmp_step0.zarr" "$out"
      else
        sk coarsen/scripts/coarsen.py -i "$W/tmp_step0.zarr" --reference-grid "$W/ec_raw.zarr" --method conservative -o "$out"
      fi
      rm -rf "$W/tmp_by_day.zarr" "$W/tmp_step0.zarr"
    done
  done
  lap "$y predictors"

  # ── Observed onset: CHIRPS on the 1.5 deg cells, this season from 15 Sep ──
  if [ ! -d "$W/obs14_$y.zarr" ]; then
    sk chirps-fetch/scripts/fetch.py --start-time $y-09-15 --end-time $((y + 1))-01-31 --bbox $CH_BOX -o "$W/chirps_$y.zarr"
    sk coarsen/scripts/coarsen.py -i "$W/chirps_$y.zarr" --reference-grid "$W/ec_raw.zarr" --method conservative -o "$W/chirps15_$y.zarr"
    for w in 7 14; do
      sk indicator/scripts/indicator.py -i "$W/chirps15_$y.zarr" --rule icpac-onset --first-in ${w}d -v precip -o "$W/obs${w}_$y.zarr"
    done
  fi
  lap "$y observed onsets"
done
for w in 7 14; do
  [ -d "$W/obs$w.zarr" ] || sk concat/scripts/concat.py $(for y in $SEASONS; do printf -- "-i %s " "$W/obs${w}_$y.zarr"; done) --dim time -o "$W/obs$w.zarr"
done

# ── 2. Leave one season out: fit on two seasons, blend the third, pool and score ──
cv() {  # $1 window, $2 set name, rest: model codes
  local w=$1 set=$2; shift 2
  local names; names=$(echo "$*" | sed 's/ec/ecmwf/; s/cu/cumulus/; s/ge/gefs/; s/ /,/g')
  local blends=()
  for test in $SEASONS; do
    local fc_train=() fc_test=()
    for m in "$@"; do
      local parts=(); for y in $SEASONS; do [ $y = $test ] || parts+=(-i "$W/p${w}_${m}_$y.zarr"); done
      sk concat/scripts/concat.py "${parts[@]}" --dim time -o "$W/cv/train_${w}_${m}_$test.zarr"
      fc_train+=(--forecast "$W/cv/train_${w}_${m}_$test.zarr"); fc_test+=(--forecast "$W/p${w}_${m}_$test.zarr")
    done
    sk mme-fit/scripts/mme_fit.py "${fc_train[@]}" --names "$names" -v probability --obs "$W/obs$w.zarr" \
      --model logistic -o "$W/cv/w_${w}_${set}_$test.zarr" 2> "$W/cv/fit_${w}_${set}_$test.log"
    sk mme-blend/scripts/mme_blend.py "${fc_test[@]}" --names "$names" -v probability \
      --weights "$W/cv/w_${w}_${set}_$test.zarr" -o "$W/cv/blend_${w}_${set}_$test.zarr"
    blends+=(-i "$W/cv/blend_${w}_${set}_$test.zarr")
  done
  sk concat/scripts/concat.py "${blends[@]}" --dim time -o "$W/cv/blend_${w}_${set}.zarr"
  echo "own samples    W=${w}d  $set: $(sk verify/scripts/verify.py --forecast "$W/cv/blend_${w}_${set}.zarr" \
    --obs "$W/obs$w.zarr" --metric brier --probability -o "$W/cv/brier_${w}_${set}.zarr" | grep verify)"
  # Weights fitted on every season, for the report.
  local fc_all=(); for m in "$@"; do
    sk concat/scripts/concat.py $(for y in $SEASONS; do printf -- "-i %s " "$W/p${w}_${m}_$y.zarr"; done) --dim time -o "$W/cv/all_${w}_${m}.zarr"
    fc_all+=(--forecast "$W/cv/all_${w}_${m}.zarr")
  done
  sk mme-fit/scripts/mme_fit.py "${fc_all[@]}" --names "$names" -v probability --obs "$W/obs$w.zarr" \
    --model logistic -o "$W/cv/w_${w}_${set}_all.zarr" 2>&1 | grep "mme-fit" | sed "s/^/weights        W=${w}d  $set: /"
}
rm -rf "$W/cv"; mkdir -p "$W/cv"
cv 7 ecmwf ec;  cv 7 cumulus cu;  cv 7 gefs ge
cv 7 ecmwf+cumulus ec cu;  cv 7 ecmwf+cumulus+gefs ec cu ge
cv 14 ecmwf ec;  cv 14 cumulus cu;  cv 14 ecmwf+cumulus ec cu
lap "cross-validation"

# ── 3. The same held-out forecasts on the samples every model shares ──
# mme-blend with each fold's own fitted weights plus weight 0 on the other models: the value equals
# the set's forecast but is NaN wherever any model is. The weights are read from mme-fit's output.
common() {  # $1 window, $2 set, $3 all models of the common sample (codes)
  local w=$1 set=$2 all=$3 blends=()
  local names; names=$(echo "$all" | sed 's/ec/ecmwf/; s/cu/cumulus/; s/ge/gefs/; s/ /,/g')
  for test in $SEASONS; do
    local args; args=$(uv run -q --with xarray --with zarr python -c "
import xarray as xr
w = xr.open_zarr('$W/cv/w_${w}_${set}_$test.zarr')
c = dict(zip(w['predictor'].values.astype(str), w['coef'].values.ravel()))
print(' '.join(f'--coef {n}={c.get(n, 0.0):.10g}' for n in '$names'.split(',')), f\"--intercept {float(w['intercept']):.10g}\")")
    local fc=(); for m in $all; do fc+=(--forecast "$W/p${w}_${m}_$test.zarr"); done
    sk mme-blend/scripts/mme_blend.py "${fc[@]}" --names "$names" -v probability --model logistic $args \
      -o "$W/common/blend_${w}_${set}_$test.zarr"
    blends+=(-i "$W/common/blend_${w}_${set}_$test.zarr")
  done
  sk concat/scripts/concat.py "${blends[@]}" --dim time -o "$W/common/blend_${w}_${set}.zarr"
  echo "common samples W=${w}d  $set: $(sk verify/scripts/verify.py --forecast "$W/common/blend_${w}_${set}.zarr" \
    --obs "$W/obs$w.zarr" --metric brier --probability -o "$W/common/brier_${w}_${set}.zarr" | grep verify)"
}
rm -rf "$W/common"; mkdir -p "$W/common"
for s in ecmwf cumulus gefs ecmwf+cumulus ecmwf+cumulus+gefs; do common 7 $s "ec cu ge"; done
for s in ecmwf cumulus ecmwf+cumulus; do common 14 $s "ec cu"; done
lap "done"

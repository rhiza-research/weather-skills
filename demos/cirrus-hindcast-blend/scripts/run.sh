#!/bin/bash
# Cirrus onset blend from Sheerwater's tiled hindcast stores, weather-skills only.
# Usage: WORK=/path/to/scratch bash run.sh   (default ./work; emptied every run)
#
# Event: the season's first ICPAC onset since 15 Sep falls within 7 days of the init.
# Predictor: each model's share of members whose first onset falls in that window.
# Inits: 15 Sep - 31 Dec 2016-2018, on dates ECMWF, Cumulus and GEFS all ran.
# Skill: leave one season out, pooled Brier skill score vs the observed base rate.
set -euo pipefail
SK=$(cd "$(dirname "$0")/../../../skills" && pwd)
W=${WORK:-$(dirname "$0")/work}; rm -rf "$W"; mkdir -p "$W"
sk() { local s=$1; shift; uv run -q "$SK/$s" "$@"; }
YEARS="2016 2017 2018"

# Hindcasts (ECMWF on its 1.5 deg cells; the 1 deg models over a wider box so every cell is covered).
sk hindcast-fetch/scripts/fetch.py --dataset ecmwf --members --start-time 2016-09-15 --end-time 2018-12-31 --bbox 7.5/28.5/-6/48 -o "$W/ec.zarr"
sk hindcast-fetch/scripts/fetch.py --dataset cumulus --start-time 2016-09-15 --end-time 2018-12-31 --bbox 9/27/-8/50 -o "$W/cu.zarr"
sk hindcast-fetch/scripts/fetch.py --dataset gefs --start-time 2016-09-15 --end-time 2018-12-31 --bbox 9/27/-8/50 -o "$W/ge.zarr"

# Observed event: CHIRPS on the same cells; --season-start restarts the count every 15 Sep.
sk chirps-fetch/scripts/fetch.py --start-time 2016-09-15 --end-time 2019-01-31 --bbox 8.25/27.75/-6.75/48.75 -o "$W/chirps.zarr"
sk coarsen/scripts/coarsen.py -i "$W/chirps.zarr" --reference-grid "$W/ec.zarr" --method conservative -o "$W/chirps15.zarr"
sk indicator/scripts/indicator.py -i "$W/chirps15.zarr" --rule icpac-onset --first-in 7d --season-start 09-15 -v precip -o "$W/obs.zarr"

# Predictors, per season: the inits all three ran (mme-fit pairs identical init dates), then each
# model's member share at step 0, on the 1.5 deg cells.
for y in $YEARS; do
  dates=$(uv run -q --with xarray --with zarr python -c "
import xarray as xr
inits = [set(xr.open_zarr('$W/' + m + '.zarr')['time'].values.astype('datetime64[D]').astype(str)) for m in ('ec', 'cu', 'ge')]
print(' '.join(f'--value {t}' for t in sorted(set.intersection(*inits)) if '$y-09-15' <= t <= '$y-12-31'))")
  for m in ec cu ge; do
    sk select/scripts/select_dim.py -i "$W/$m.zarr" --dim time $dates -o "$W/${m}_$y.zarr"
    sk indicator/scripts/indicator.py -i "$W/${m}_$y.zarr" --rule icpac-onset --first-in 7d --season-start 09-15 \
      --probability --time-dim step -v precip -o "$W/${m}_${y}_share.zarr"
    sk select/scripts/select_dim.py -i "$W/${m}_${y}_share.zarr" --dim step --index 0 -o "$W/p_${m}_$y.zarr"
    [ $m = ec ] || sk coarsen/scripts/coarsen.py -i "$W/p_${m}_$y.zarr" --reference-grid "$W/ec.zarr" \
      --method conservative -o "$W/p_${m}_${y}_1p5.zarr"
  done
done
p() { [ $1 = ec ] && echo "$W/p_ec_$2.zarr" || echo "$W/p_${1}_${2}_1p5.zarr"; }   # model, year

# Leave one season out: fit on the other two, blend the held-out one; score all held-out together.
for set in ec cu ge "ec cu ge"; do
  names=$(echo "$set" | sed 's/ec/ecmwf/; s/cu/cumulus/; s/ge/gefs/; s/ /,/g')
  held=()
  for test in $YEARS; do
    train=(); blend=()
    for m in $set; do
      sk concat/scripts/concat.py $(for y in $YEARS; do [ $y = $test ] || echo -i "$(p $m $y)"; done) --dim time -o "$W/train_$m.zarr"
      train+=(--forecast "$W/train_$m.zarr"); blend+=(--forecast "$(p $m $test)")
    done
    sk mme-fit/scripts/mme_fit.py "${train[@]}" --names "$names" -v probability --obs "$W/obs.zarr" --model logistic -o "$W/w.zarr"
    sk mme-blend/scripts/mme_blend.py "${blend[@]}" --names "$names" -v probability --weights "$W/w.zarr" -o "$W/blend_$test.zarr"
    held+=(-i "$W/blend_$test.zarr")
  done
  sk concat/scripts/concat.py "${held[@]}" --dim time -o "$W/heldout.zarr"
  echo "$names: $(sk verify/scripts/verify.py --forecast "$W/heldout.zarr" --obs "$W/obs.zarr" --metric brier --probability -o "$W/brier.zarr" | grep verify)"
done

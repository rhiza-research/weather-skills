#!/bin/bash
# One-time download: daily TAHMO observations (rainfall, temperature, humidity, pressure) for every TA
# station in the East African (ICPAC) countries, one file per year into $ARCHIVE/tahmo/tahmo_YYYY.zarr.
# Needs TAHMO_API_USERNAME and TAHMO_API_PASSWORD.
#   TAHMO_COUNTRIES  ISO alpha-2 codes to keep (default: the 11 ICPAC countries)
#   TAHMO_FIRST_YEAR first year (default 2015)
# A year appears under its final name only once complete, and an existing year is never fetched
# again, including the current one (which ends the day before it was downloaded): delete a year's
# file to refresh it.
set -uo pipefail
COUNTRIES=${TAHMO_COUNTRIES:-"BI DJ ER ET KE RW SD SO SS TZ UG"}
FIRST=${TAHMO_FIRST_YEAR:-2015}
THIS=$(date +%Y)
mkdir -p "$ARCHIVE/tahmo" && cd "$ARCHIVE/tahmo"

# Station list: every TA station in a box around the region, kept by country.
if [ ! -s stations.tsv ]; then
  uv run -q "$SKILLS/tahmo-fetch/scripts/fetch.py" --list-stations --bbox 23/21/-12.5/52 2>/dev/null > stations_all.tsv
  # Station rows only (the listing also prints "API request" lines): id starts with TA, country kept.
  awk -F'\t' -v keep="$COUNTRIES" 'BEGIN { n = split(keep, c, " "); for (i = 1; i <= n; i++) ok[c[i]] = 1 }
    $1 ~ /^TA[0-9]/ && ($5 in ok)' stations_all.tsv > tmp_stations.tsv && mv tmp_stations.tsv stations.tsv
  rm -f stations_all.tsv
fi
ids=(); while IFS=$'\t' read -r id _; do ids+=(--station "$id"); done < stations.tsv
echo "tahmo: $(( ${#ids[@]} / 2 )) stations in $COUNTRIES"

skipped=0
for y in $(seq $FIRST $THIS); do
  [ -d tahmo_$y.zarr ] && { skipped=$((skipped + 1)); continue; }
  end=$y-12-31; [ $y = $THIS ] && end=$(date -v-1d +%Y-%m-%d)
  rm -rf tmp_tahmo_$y.zarr
  s=$(date +%s)
  uv run -q "$SKILLS/tahmo-fetch/scripts/fetch.py" "${ids[@]}" --start-time $y-01-01 --end-time $end \
    --workers 8 -o tmp_tahmo_$y.zarr > tahmo_$y.log 2>&1 \
  && mv tmp_tahmo_$y.zarr tahmo_$y.zarr \
  && echo "ok $y ($(( $(date +%s) - s ))s)" || echo "FAIL $y (see $ARCHIVE/tahmo/tahmo_$y.log)"
done
echo "tahmo: $skipped years already downloaded, skipped"

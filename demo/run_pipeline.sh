#!/usr/bin/env bash
# End-to-end demo: a daily S2S rainfall pipeline (ECMWF forecast, IMERG and
# CHIRPS observations, TAHMO stations, weekly and dekadal totals per country)
# using the weather-skills CLI. Requires ECMWF_DATASTORES_KEY, EARTHDATA_*,
# TAHMO_API_*. Plotting is not part of this repo, so the demo stops at Zarrs.

set -euo pipefail

# Resolve cwd to the directory containing this script so relative paths in the
# pipeline (./demo_output, ../, etc.) and direnv's .envrc load consistently
# regardless of the caller's PWD.
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"
eval "$(direnv export bash 2>/dev/null || true)"

days_ago() { date -u -d "$1 days ago" +%Y-%m-%d 2>/dev/null || date -u -v-"$1"d +%Y-%m-%d; }
date_minus() { date -u -d "$1 - $2 days" +%Y-%m-%d 2>/dev/null || date -u -j -v-"$2"d -f "%Y-%m-%d" "$1" +%Y-%m-%d; }
# Positional args: INIT_DATE END_DATE OUT
# Defaults: INIT = two days ago, END = today.
INIT_DATE="${1:-$(days_ago 2)}"
END_DATE="${2:-$(days_ago 0)}"
START_DATE=$(date_minus "$END_DATE" 60)
OUT="${3:-./demo_output}"
COUNTRIES=(Kenya Ghana Senegal Ethiopia)
declare -A ISO=([Kenya]=KEN [Ghana]=GHA [Senegal]=SEN [Ethiopia]=ETH)

mkdir -p "$OUT"
uv tool install --force --reinstall ../

# Africa is a continent, not a country, so the continental extent is passed as an
# explicit bbox rather than via resolve-region (which is country-level).
weather-skills ecmwf-fetch  --date  "$INIT_DATE"  --bbox=23/-20/-37/59 --output "$OUT/ecmwf.zarr"
weather-skills imerg-fetch  --start-time "$START_DATE" --end-time "$END_DATE" --output "$OUT/imerg.zarr"
weather-skills chirps-fetch --start-time "$START_DATE" --end-time "$END_DATE" --output "$OUT/chirps.zarr"

for COUNTRY in "${COUNTRIES[@]}"; do
    d="$OUT/$COUNTRY"
    mkdir -p "$d"

    # Resolve the country once: capture the bbox on stdout and write the boundary
    # polygon to boundary.geojson (the --geojson status note goes to stderr, so the
    # command substitution captures only the bbox).
    BBOX=$(weather-skills resolve-region "${ISO[$COUNTRY]}" --geojson "$d/boundary.geojson")

    weather-skills clip-region        -i "$OUT/ecmwf.zarr"          -o "$d/ecmwf.zarr"         --bbox="$BBOX"
    weather-skills aggregate-temporal -i "$d/ecmwf.zarr"    -o "$d/ecmwf_weekly.zarr"  --period weekly  --method mean
    weather-skills aggregate-temporal -i "$d/ecmwf.zarr"    -o "$d/ecmwf_dekadal.zarr" --period dekadal --method mean
    weather-skills convert-to-totals  -i "$d/ecmwf_weekly.zarr"  -o "$d/ecmwf_weekly_totals.zarr"
    weather-skills convert-to-totals  -i "$d/ecmwf_dekadal.zarr" -o "$d/ecmwf_dekadal_totals.zarr"
    # Dekadal forecast downscaled to 0.25°.
    weather-skills downscale          -i "$d/ecmwf_dekadal.zarr" -o "$d/ecmwf_dekadal_ds.zarr" --algorithm linear-interpolation --target-resolution 0.25 --variable tp
    weather-skills convert-to-totals  -i "$d/ecmwf_dekadal_ds.zarr" -o "$d/ecmwf_dekadal_ds_totals.zarr"

    weather-skills clip-region        -i "$OUT/imerg.zarr"  -o "$d/imerg.zarr"         --bbox="$BBOX"
    weather-skills aggregate-temporal -i "$d/imerg.zarr"    -o "$d/imerg_weekly.zarr"  --period weekly  --method mean --end-time "$END_DATE"
    weather-skills aggregate-temporal -i "$d/imerg.zarr"    -o "$d/imerg_dekadal.zarr" --period dekadal --method mean --end-time "$END_DATE"
    weather-skills convert-to-totals  -i "$d/imerg_weekly.zarr"  -o "$d/imerg_weekly_totals.zarr"
    weather-skills convert-to-totals  -i "$d/imerg_dekadal.zarr" -o "$d/imerg_dekadal_totals.zarr"

    weather-skills clip-region        -i "$OUT/chirps.zarr" -o "$d/chirps.zarr"         --bbox="$BBOX"
    weather-skills aggregate-temporal -i "$d/chirps.zarr"   -o "$d/chirps_weekly.zarr"  --period weekly  --method mean --end-time "$END_DATE"
    weather-skills aggregate-temporal -i "$d/chirps.zarr"   -o "$d/chirps_dekadal.zarr" --period dekadal --method mean --end-time "$END_DATE"
    weather-skills convert-to-totals  -i "$d/chirps_weekly.zarr"  -o "$d/chirps_weekly_totals.zarr"
    weather-skills convert-to-totals  -i "$d/chirps_dekadal.zarr" -o "$d/chirps_dekadal_totals.zarr"

    weather-skills tahmo-fetch --bbox="$BBOX" --start-time "$START_DATE" --end-time "$END_DATE" --output "$d/tahmo.zarr"

    # Station data aggregated to the same windows as the IMERG/CHIRPS totals,
    # ready to compare against them.
    weather-skills aggregate-temporal -i "$d/tahmo.zarr" -o "$d/tahmo_weekly.zarr"  --variable precip --period weekly  --method mean --end-time "$END_DATE"
    weather-skills aggregate-temporal -i "$d/tahmo.zarr" -o "$d/tahmo_dekadal.zarr" --variable precip --period dekadal --method mean --end-time "$END_DATE"
    weather-skills convert-to-totals  -i "$d/tahmo_weekly.zarr"  -o "$d/tahmo_weekly_totals.zarr"  --variable precip
    weather-skills convert-to-totals  -i "$d/tahmo_dekadal.zarr" -o "$d/tahmo_dekadal_totals.zarr" --variable precip
done

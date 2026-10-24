---
name: rfe-fetch
description: "Fetch NOAA CPC African Rainfall Estimate (RFE2) daily rainfall for a date range and region into a weather-skills Zarr. Credential-free, 0.1° over Africa, daily from 2001, about one day behind real time. Use when a task needs RFE2 rainfall, such as a gauge-merged satellite estimate to compare with CHIRPS, IMERG, stations, or a forecast."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public files from ftp.cpc.ncep.noaa.gov over HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.3"
  catalog-group: fetchers
  variables:
    - precip
---

# rfe-fetch

Downloads RFE2 daily rainfall for Africa from CPC's FEWS NET archive, one GeoTIFF per day, as one `precip` variable.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive range of days, `YYYY-MM-DD`, from 2001-01-01.
- `--bbox` — `N/W/S/E` inside 40N/20W/40S/55E. Omit for the whole Africa grid (801 × 751 cells). A box outside the grid exits 1.
- `--probe-latest` — print the latest published day (`YYYY-MM-DD`) and exit; reads only the directory listing.
- `--output`, `-o` — output Zarr.

### Output
`precip` on `(time, latitude, longitude)` in `mm day-1`, 0.1° cells centred on whole tenths, latitude ascending. Each `time` is a day at 00:00 whose value is the rainfall from 06Z that day to 06Z the next, so days line up with CHIRPS on `time` (with a six-hour offset in the actual window). Ocean cells have satellite-only values.

### Notes
- CPC publishes each day the following afternoon UTC. For a recent window, end on `--probe-latest`, not today.
- Days CPC has no file for are left out, never filled; stderr lists them. No published day in the range exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-07-01 --end-time 2026-09-30 \
  --bbox 5.5/33.9/-4.7/41.9 -o rfe_kenya.zarr
```
Writes July–September 2026 daily RFE2 rainfall over Kenya, ready to aggregate to monthly totals.

## Details

RFE2 merges GTS rain gauges with infrared (GPI) and microwave (SSM/I, AMSU) satellite estimates. The grid is read from each file's geotransform; if it ever differs between days, the skill exits 1 rather than mixing grids. CPC's missing marker (-999) becomes NaN. Known archive gaps include 2007-11-13, 2023-07-25, 2023-12-28 and 2024-08-06. CPC notes that a few pixels on some days in 2005 exceed 300 mm/day; they pass through unchanged.

After aggregate-temporal, a window with a missing day has `aggregation_coverage` below 1, so convert-to-totals (default `--min-coverage 1.0`) drops it.

Each day is a ~0.7 MB download, fetched 8 at a time and clipped to `--bbox` as it arrives; three months over Kenya take about 15 s.

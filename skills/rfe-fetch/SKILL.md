---
name: rfe-fetch
description: Fetch NOAA CPC African Rainfall Estimate (RFE2) daily precipitation for a date range and optional `--bbox` from CPC's credential-free FEWS NET server, and write a weather-skills standard dataset Zarr. 0.1 degree grid over Africa (40S-40N, 20W-55E), daily from 2001-01-01, about one day behind real time. Each day is the 06Z-06Z total starting on that date. Use when a task needs RFE2 rainfall, e.g. as a gauge-merged satellite estimate to compare with CHIRPS, IMERG, stations, or a forecast. For the latest day run `--probe-latest` first.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public files from ftp.cpc.ncep.noaa.gov over HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
---

# rfe-fetch

Downloads NOAA Climate Prediction Center (CPC) Rainfall Estimator version 2
(RFE2) daily rainfall for Africa from the FEWS NET archive
(`https://ftp.cpc.ncep.noaa.gov/fews/fewsdata/africa/rfe2/bin/`, one gzipped
binary file per day). The archive also has a GeoTIFF copy of each day, with the
same values. RFE2 merges GTS rain gauges with GPI (infrared), SSM/I and AMSU
(microwave) satellite estimates. The skill writes a standard-dataset Zarr with
one `precip` variable. No credentials or registration.

## When to use

- Daily gridded rainfall over Africa, recent (about one day behind) or
  historical back to 2001.
- As an independent cross-check on CHIRPS (`chirps-fetch`) or IMERG
  (`dynamical-fetch`), with different satellite inputs and gauge merging.

Not for outside Africa (the grid stops at 40S-40N, 20W-55E), and not before
2001-01-01 (exit 2).

## Recent windows: probe first

CPC publishes each day the following afternoon UTC. For "the last two weeks",
end the window on the latest published day, not today:

```bash
END=$(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest)
T=$(uv run ${CLAUDE_SKILL_DIR}/../resolve-time/scripts/resolve.py last-2w --as-of "$END" --emit iso)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time "${T%/*}" --end-time "${T#*/}" \
    --bbox 5/34/-5/42 -o /tmp/rfe_2w.zarr
```

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD \
    [--bbox N/W/S/E] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments

- `--start-time`, `--end-time` — inclusive range of file dates (`YYYY-MM-DD`).
- `--bbox` — `N/W/S/E` subset of the Africa grid. Omit for the whole grid
  (801 × 751 cells). A box that misses 40N/20W/40S/55E entirely exits 2. Use
  `resolve-region` to turn a country into this value.
- `--probe-latest` — print the latest published `YYYY-MM-DD` on stdout and
  exit. It reads only the directory listing. Takes no argument and no `-o`.
- `--output`, `-o` — output Zarr path.

### Output

Zarr with dims `(time, latitude, longitude)` and data variable `precip`
(`mm day-1`). The source values are 24 h totals in mm, so they are already
daily rates. The grid is 0.1°, with cell centres on whole tenths (e.g. 33.9,
-4.6). Latitude is ascending. CPC's missing marker (-999) is NaN. Ocean cells
have values (satellite only, no gauges). Stamped with
`weather_skills_source=noaa-cpc-rfe2` and `data_interval` `1 day`.

**Day window.** Each `time` is the file date at 00:00, but the value is the
rainfall from 06Z on that date to 06Z the next day. That is CPC's FEWS
convention, documented for its sibling ARC2 product. The day is labeled like
CHIRPS days, so the two line up on `time` for `difference` or `verify`. The
two windows are still offset by six hours.

**Missing days.** A day CPC has no file for (archive gaps such as 2004-07-24,
2007-11-13, 2019-06-16, or days after the latest published one) is an absent
row, never filled. Stderr lists every one. If no day in the range is
published, the skill exits 1. After `aggregate-temporal`, a window with a
missing day has `aggregation_coverage` < 1, so `convert-to-totals` (default
`--min-coverage 1.0`) drops it.

**Known caveat.** CPC's readme says a few pixels on some days in 2005 exceed
300 mm/day. They are passed through unchanged.

### Performance

Each day is a ~0.5 MB download, fetched 8 at a time. Three months over Kenya
take about 15 s. Each day is clipped to `--bbox` as it arrives, so long
windows over a small region stay small in memory.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

```bash
# Kenya, July-September 2026, then monthly totals.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
    --start-time 2026-07-01 --end-time 2026-09-30 \
    --bbox 5.506/33.894/-4.677/41.855 -o /tmp/rfe_kenya.zarr
uv run ${CLAUDE_SKILL_DIR}/../aggregate-temporal/scripts/aggregate.py \
    -i /tmp/rfe_kenya.zarr -o /tmp/rfe_kenya_monthly.zarr --period monthly
```

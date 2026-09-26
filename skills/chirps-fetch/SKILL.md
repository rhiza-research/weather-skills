---
name: chirps-fetch
description: Fetch CHIRPS precipitation observations for a date range and optional region from the dynamical.org catalog — the validated final product from 1981 and a preliminary fallback for very recent days — and write a weather-skills standard dataset Zarr. Use when a task needs CHIRPS rainfall, recent or historical, e.g. to compare against a forecast or station data, or to build a reference period. Pass --bbox N/W/S/E to slice the 0.05° land grid in space.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public Icechunk Zarr from the dynamical.org open catalog (`ucsb-chc-chirps-analysis-final` / `ucsb-chc-chirps-analysis-preliminary`); no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - precip
---

# chirps-fetch

Opens CHIRPS v3.0 daily precipitation from the [dynamical.org](https://dynamical.org/catalog/)
catalog and writes a weather-skills standard dataset Zarr. The validated
**final** product (`ucsb-chc-chirps-analysis-final`) covers **1981–present**
(including the reanalysis-disaggregated daily archive). Days the final has
not published yet come from **preliminary**
(`ucsb-chc-chirps-analysis-preliminary`). When both exist for a day, final
is used.

`--bbox N/W/S/E` subsets the 0.05° land grid (60°S–60°N) in space before
bytes are pulled. Omit it for the full native grid. Country bboxes come from
`resolve-region`.

To fetch one product without the final/prelim merge, use `dynamical-fetch`
`--dataset ucsb-chc-chirps-analysis-final` (or `…-preliminary`) and
`-v precipitation_surface`. This skill is the default CHIRPS path: it merges
the two and writes `precip`.

## When to use

- A task needs CHIRPS rainfall as gridded observations — recent days, a historical period, or a reference/normal year (final from 1981).
- A downstream skill will clip, aggregate, or compare CHIRPS against other sources.

Coverage starts in 1981. Dates before 1981 are unavailable and exit non-zero.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD \
    [--bbox N/W/S/E] --output <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`. Calendar windows: `resolve-time last-2w`. Latest published day: `--probe-latest` (then `--as-of` on resolve-time to end a rolling window there).
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. Omit for the full native 0.05° land grid (60°S–60°N). To fetch over a country, get its bbox from `resolve-region`.
- `--probe-latest` — print the latest available `YYYY-MM-DD` on stdout and exit. No `-o`. Reads catalog time coordinates only.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — ignored; retained so existing scripts that pass it still run.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, latitude, longitude)` on the CHIRPS grid (native, or the `--bbox` slice). Stamped with `weather_skills_source=chirps` and `data_interval` `1 day` (no `aggregation_period` until `aggregate-temporal`). Catalog precip is `precipitation_surface` in `kg m-2 s-1`; this skill converts and renames.

### Memory and performance

The native 0.05° land grid is 7200×2400 cells. Pass `--bbox` so the catalog
read is windowed to the region — that is the memory and network lever. Keep
long global windows short on tight-memory hosts; `clip-region` can trim
further after fetch.

### Production lag and partial-tail behavior

Historical days come from the validated final product and carry no publication lag; the lag and partial-tail behavior described here apply only to the recent tail, which is served by the preliminary product. The CHIRPS v3.0 daily preliminary product is published on a pentad-based schedule: per-day files appear in batches **2 days after each pentad closes** (pentads end on the 5th, 10th, 15th, 20th, 25th, and last day of each month). Best-case lag is 2 days (the last day of a pentad, published 2 days later); worst case is ~7 days (the day right after a pentad ends, which waits for the next pentad to close before its batch is published). Average lag is 4-5 days. See https://www.chc.ucsb.edu/data/chirps3 for the official schedule. When the requested `--end-time` falls inside the lag window, the script writes a partial dataset covering only the days that were available on the server, logs the missing days and effective end date to stderr, and exits 0. If days are missing from the middle of the range (not the tail), the script exits 2 — that's a server-side data gap, not a lag issue.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="chirps-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records the run's flag
values under underscored names (e.g. a flag `--time-dim` is recorded as
`time_dim`); `version` is the value printed by `--help`. Inspect a written
output's provenance with the `provenance` skill.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
    --start-time 2026-01-01 --end-time 2026-02-15 \
    --bbox 5/34/-5/42 \
    --output /tmp/chirps.zarr
```

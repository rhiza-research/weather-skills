---
name: chirps-fetch
description: Fetch CHIRPS precipitation observations for a date range and optional region from the dynamical.org catalog — the validated final product from 1981 and a preliminary fallback for very recent days — and write a weather-skills standard dataset Zarr. Use when a task needs CHIRPS rainfall, recent or historical, e.g. to compare against a forecast or station data, or to build a reference period. Pass --bbox N/W/S/E to slice the 0.05° land grid in space. Recent days lag 2-7 days, so for the last N days or weeks run --probe-latest first and end the window there (resolve-time QUERY --as-of LATEST); a window past the latest day is cut short with only a stderr warning (exit 2 if none of it is published).
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
**final** product (`ucsb-chc-chirps-analysis-final`) covers **1981-01-01**
to the end of a recent month (about 1-2 months behind). Days the final has
not published yet come from **preliminary**
(`ucsb-chc-chirps-analysis-preliminary`, from 2025-01-01). When both exist for
a day, final is used. Both share the same grid, so the seam has no shift.

`--bbox N/W/S/E` subsets the 0.05° land grid (60°S–60°N) in space before
bytes are pulled. Omit it for the full native grid. Country bboxes come from
`resolve-region`.

To fetch one product without the final/prelim merge, use `dynamical-fetch`
`--dataset ucsb-chc-chirps-analysis-final` (or `…-preliminary`) and
`-v precipitation_surface`. This skill is the default CHIRPS path: it merges
the two and writes `precip`.

## Recent windows: probe first

For "the last two weeks", "last week", "last dekad" of CHIRPS, end the window
on the latest published day, not today. Otherwise the tail is cut and a
"two-week" map silently holds only the days published so far.

```bash
END=$(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest)
T=$(uv run skills/resolve-time/scripts/resolve.py last-2w --as-of "$END" --emit iso)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time "${T%/*}" --end-time "${T#*/}" \
    --bbox 5/34/-5/42 -o /tmp/chirps_2w.zarr
```

`--as-of` works for calendar periods too: `last-week`, `last-dekad`,
`last-month` then give the last complete period on or before the probed day.
The `"${T%/*}"` / `"${T#*/}"` form works in bash and zsh (zsh does not
word-split an unquoted `$TIME` of flags).

## When to use

- A task needs CHIRPS rainfall as gridded observations — recent days, a historical period, or a reference/normal year (final from 1981).
- A downstream skill will clip, aggregate, or compare CHIRPS against other sources.

Coverage starts 1981-01-01. A `--start-time` before that exits 2 with `Error:`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD \
    [--bbox N/W/S/E] --output <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`. Recent windows: `--probe-latest`, then `resolve-time <query> --as-of <latest>` (see above).
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. Omit for the full native 0.05° land grid (60°S–60°N). To fetch over a country, get its bbox from `resolve-region`.
- `--probe-latest` — print the latest available `YYYY-MM-DD` (final or preliminary) on stdout and exit. Takes no argument and no `-o`. Reads catalog time coordinates only. stderr says which product it is, e.g. `latest=2026-10-05 (preliminary); final through 2026-08-31`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — ignored; retained so existing scripts that pass it still run.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, latitude, longitude)` on the CHIRPS grid (native, or the `--bbox` slice). Each `time` is the 24 h starting at 00 UTC that day. Cell centres sit at odd multiples of 0.025° (e.g. 5.475, 33.925), stored float32, latitude ascending; `--bbox` keeps the centres inside it, edges inclusive. Oceans are NaN. Stamped with `weather_skills_source=chirps` and `data_interval` `1 day` (no `aggregation_period` until `aggregate-temporal`). Catalog precip is `precipitation_surface` in `kg m-2 s-1`; this skill converts and renames.

### Memory and performance

The native 0.05° land grid is 7200×2400 cells. Pass `--bbox` so the catalog
read is windowed to the region — that is the memory and network lever. Store
chunks are 365 days × 60×60 cells, so even one day reads whole-year chunks
over the bbox (a Kenya bbox takes ~5 s); without `--bbox` that is every chunk
of the globe for each year touched. Keep
long global windows short on tight-memory hosts; `clip-region` can trim
further after fetch.

### Production lag and partial-tail behavior

Historical days come from the validated final product and carry no publication lag; the lag and partial-tail behavior described here apply only to the recent tail, which is served by the preliminary product. The CHIRPS v3.0 daily preliminary product is published on a pentad-based schedule: per-day files appear in batches **2 days after each pentad closes** (pentads end on the 5th, 10th, 15th, 20th, 25th, and last day of each month). Best-case lag is 2 days (the last day of a pentad, published 2 days later); worst case is ~7 days (the day right after a pentad ends, which waits for the next pentad to close before its batch is published). Average lag is 4-5 days. See https://www.chc.ucsb.edu/data/chirps3 for the official schedule. When the requested `--end-time` falls inside the lag window, the script writes a partial dataset covering only the days that were available on the server, logs the missing days and effective end date to stderr, and exits 0. If no requested day is published yet, it exits 2. If days are missing from the middle of the range (not the tail), the script exits 2 — that's a server-side data gap, not a lag issue. Missing days are never filled.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
    --start-time 2026-01-01 --end-time 2026-02-15 \
    --bbox 5/34/-5/42 \
    --output /tmp/chirps.zarr
```

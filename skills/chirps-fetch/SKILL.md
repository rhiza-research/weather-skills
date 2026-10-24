---
name: chirps-fetch
description: "Fetch CHIRPS daily rainfall observations (0.05° land grid, 1981 to a few days ago) for a date range and optional region from the dynamical.org catalog, as a weather-skills Zarr. Joins the validated final product with the preliminary one for recent days into one `precip` series. Use when a task needs CHIRPS rainfall, recent or historical."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads public Icechunk Zarr from the dynamical.org open catalog (`ucsb-chc-chirps-analysis-final` / `ucsb-chc-chirps-analysis-preliminary`); no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - precip
---

# chirps-fetch

Fetches CHIRPS v3.0 daily precipitation, using the final product where it exists and the preliminary product for later days.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o OUT.zarr
# Latest published day (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Coverage starts 1981-01-01; an earlier start exits 2.
- `--bbox` — `N/W/S/E` decimal degrees; keeps cell centres inside, edges inclusive. Omit for the full 60°S–60°N land grid.
- `--probe-latest` — print the latest published day (`YYYY-MM-DD`) on stdout and exit. Takes no value. stderr names the product, e.g. `latest=2026-10-10 (preliminary); final through 2026-08-31`.
- `--workers` — ignored. Default: 8.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `precip` in `mm day-1`, dims `(time, latitude, longitude)`, latitude ascending, float32.
- Each `time` is the 24 h starting at 00 UTC that day. Cell centres sit at odd multiples of 0.025° (e.g. 5.475, 33.925). Oceans are NaN.
- `weather_skills_source=chirps`, `data_interval` `1 day`.

### Notes
- Recent days are published in batches 2 days after each pentad (5-day block ending on the 5th, 10th, 15th, 20th, 25th and last day of the month), so the latest day is 2–7 days old. For "the last N days", run `--probe-latest` and end the window on that day.
- An `--end-time` past the latest day writes the days available and warns on stderr with the effective end. No published day in the range exits 2. A missing day inside the range exits 2. Missing days are never filled.
- Always pass `--bbox` for long windows: data is stored in 365-day × 60×60-cell chunks, so each year touched reads every chunk over the bbox.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-09-01 --end-time 2026-09-30 \
  --bbox 5.5/33.5/-5/42.5 -o chirps_kenya_sep.zarr
```
Writes 30 daily CHIRPS rainfall fields over Kenya for September 2026.

## Details

The final product (`ucsb-chc-chirps-analysis-final`) runs from 1981 to the end of a recent month, a month or two behind. The preliminary product (`ucsb-chc-chirps-analysis-preliminary`, from 2025) fills the days after that. Both share one grid, so there is no shift at the join. To read just one of them, fetch it by catalog id from dynamical.org directly.

---
name: imerg-fetch
description: "Fetch NASA IMERG daily precipitation (global 0.1° grid) for a date range from NASA GES DISC via Earthdata login, as a weather-skills Zarr. Serves the daily Late (GPM_3IMERGDL, about 2 days behind) and Final (GPM_3IMERGDF, gauge-adjusted research quality) files. Use only for IMERG Final. For IMERG Late or Early, recent rainfall, or any day after Final ends, use dynamical-fetch instead: it needs no login, reads only the box, and covers 1998 to now."
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`. Earthdata accounts are free with sign-up at https://urs.earthdata.nasa.gov/users/new.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.5"
  catalog-group: fetchers
  variables:
    - precip
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# imerg-fetch

Downloads IMERG daily files for a date range and writes the daily precipitation grid, globally or cut to `--bbox`.

**Use dynamical-fetch first.** Its `nasa-imerg-analysis-late` and `nasa-imerg-analysis-early` stores cover 1998 to a few hours ago with no login, and read only the `--bbox` cells; aggregate-temporal turns their half-hourly steps into daily means. Use imerg-fetch only for IMERG Final (gauge-adjusted, V07), which dynamical.org does not serve. Do not use it for Late, or as a fallback before trying dynamical-fetch.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--version late|final] [--bbox N/W/S/E] -o OUT.zarr
# Latest published day of one product (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [late|final]
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range.
- `--version` — `late` (`GPM_3IMERGDL`, near real time) or `final` (`GPM_3IMERGDF`). Default: `late`.
- `--bbox` — `N/W/S/E` decimal degrees; W > E crosses the antimeridian. Default: the whole globe.
- `--probe-latest` — print the latest published day (`YYYY-MM-DD`) on stdout and exit. Optional value `late` or `final`; without one it uses `--version`.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `precip`, the daily mean rate in `mm day-1`, dims `(time, latitude, longitude)`, latitude ascending, on the 0.1° grid (global, or the cells inside `--bbox`).
- `data_interval` `1 day`. Global attrs `weather_skills_source=imerg`, `imerg_version` (`late` or `final`), and `source` naming the GES DISC collection.

### Notes
- Credentials come from `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`, then `~/.netrc`; failure exits 1.
- Final lags months (its V07 collection ends 2025-09-30 as of October 2026); run `--probe-latest final` and use `final` only up to that day. Days after it come from dynamical-fetch `nasa-imerg-analysis-late`, so a window crossing that day is two fetches. A `final` window with no published day exits 1.
- `--version late` is kept for when dynamical-fetch fails; try dynamical-fetch first.
- An `--end-time` past the latest day writes the days available and warns on stderr. A missing day inside the range exits 1. Missing days are never filled.
- Each day still downloads the whole global file (about 32 MB), one at a time, deleted once it is cut to `--bbox`; long windows take time but not disk. Without `--bbox` the output holds about 26 MB per day in memory, so pass `--bbox` for anything longer than a few weeks.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2025-03-01 --end-time 2025-05-31 --version final --bbox 5.5/33.5/-5/42.5 -o imerg_final_mam2025.zarr
```
Writes 92 days of IMERG Final daily rainfall rates over Kenya.

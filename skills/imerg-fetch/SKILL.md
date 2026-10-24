---
name: imerg-fetch
description: "Fetch NASA IMERG daily precipitation (global 0.1° grid) for a date range from NASA GES DISC via Earthdata login, as a weather-skills Zarr. Serves the daily Late (GPM_3IMERGDL, about 2 days behind) and Final (GPM_3IMERGDF, gauge-adjusted research quality) files. Use when a task specifically needs these daily GES DISC products, for example IMERG Final."
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`. Earthdata accounts are free with sign-up at https://urs.earthdata.nasa.gov/users/new.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
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

Downloads IMERG daily files for a date range and writes the global daily precipitation grid.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--version late|final] -o OUT.zarr
# Latest published day of one product (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [late|final]
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range.
- `--version` — `late` (`GPM_3IMERGDL`, near real time) or `final` (`GPM_3IMERGDF`). Default: `late`.
- `--probe-latest` — print the latest published day (`YYYY-MM-DD`) on stdout and exit. Optional value `late` or `final`; without one it uses `--version`.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `precip`, the daily mean rate in `mm day-1`, dims `(time, latitude, longitude)`, latitude ascending, on the full global 0.1° grid.
- `data_interval` `1 day`. Global attrs `weather_skills_source=imerg`, `imerg_version` (`late` or `final`), and `source` naming the GES DISC collection.

### Notes
- Credentials come from `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`, then `~/.netrc`; failure exits 1.
- Pick `--version` from the window. Final lags months (its V07 collection ends 2025-09-30 as of October 2026); run `--probe-latest final` and use `final` only up to that day, `late` after it. A window crossing that day needs two fetches, or `late` throughout. A `final` window with no published day exits 1.
- An `--end-time` past the latest day writes the days available and warns on stderr. A missing day inside the range exits 1. Missing days are never filled.
- There is no `--bbox`: each day downloads the whole global file (about 32 MB) and holds it in memory. Keep windows short and clip afterwards.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-10-01 --end-time 2026-10-10 --version late -o imerg_late_oct.zarr
```
Writes 10 days of global IMERG Late daily rainfall rates.

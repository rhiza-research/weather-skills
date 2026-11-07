---
name: imerg-fetch
description: "Fetch NASA IMERG satellite precipitation (global 0.1° grid) for a date range and optional region, as a weather-skills Zarr: Early or Late half-hourly, or as daily means with --daily, from the dynamical.org catalog (no login, 1998 to a few hours ago), or Final daily (gauge-adjusted research quality) from NASA GES DISC with an Earthdata login. Late falls back to the GES DISC daily files when dynamical.org fails. Use for any IMERG rainfall, recent or historical."
license: MIT
compatibility: Requires Python 3.12 and uv. Early and Late need no credentials. Final, and the Late fallback, authenticate to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`. Earthdata accounts are free with sign-up at https://urs.earthdata.nasa.gov/users/new.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.8"
  catalog-group: fetchers
  variables:
    - precip
  openclaw:
    envVars:
      - name: EARTHDATA_USERNAME
        description: NASA Earthdata login (free at https://urs.earthdata.nasa.gov/users/new). Needed only for --version final and the Late fallback.
      - name: EARTHDATA_PASSWORD
        description: NASA Earthdata password. Needed only for --version final and the Late fallback.
---

# imerg-fetch

Fetches NASA IMERG precipitation for a date range, globally or cut to `--bbox`. Each `--version` has one default source; only Late has a fallback.

| `--version` | Default source | Fallback | Interval | Login |
|---|---|---|---|---|
| `early` | dynamical.org `nasa-imerg-analysis-early` | none | 30 minute | no |
| `late` (default) | dynamical.org `nasa-imerg-analysis-late` | NASA GES DISC `GPM_3IMERGDL` | 30 minute (fallback: 1 day) | no (fallback: Earthdata) |
| `final` | NASA GES DISC `GPM_3IMERGDF` | none | 1 day | Earthdata |

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--version early|late|final] [--bbox N/W/S/E] [--daily] -o OUT.zarr
# Latest published day of one product (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [early|late|final]
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range; the end day keeps all its half-hourly steps.
- `--version` — `early`, `late` or `final` (see the table). Default: `late`.
- `--daily` — Early and Late: write one mean per UTC day instead of every half hour, reading whole store chunks (three months) at a time, about 22 s per year for a country box. Use it for any daily or longer work: 1998 to now is one call at 1/48 of the half-hourly size, so never split IMERG into multi-year pieces and aggregate each. Same values as `aggregate-temporal --period daily` on the half-hourly fetch. Final and the Late fallback are already daily.
- `--bbox` — `N/W/S/E` decimal degrees; W > E crosses the antimeridian. Default: the whole globe.
- `--probe-latest` — print the latest published day (`YYYY-MM-DD`) on stdout and exit. Optional value `early`, `late` or `final`; without one it uses `--version`. Early and Late read the dynamical.org catalog and note on stderr when that day is still partial; Final asks GES DISC (Earthdata login).
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `precip`, the mean precipitation rate in `mm day-1`, dims `(time, latitude, longitude)`, latitude ascending, on the 0.1° grid (global, or the cells inside `--bbox`).
- `data_interval` `30 minute` from dynamical.org, `1 day` from GES DISC or with `--daily`.
- With `--daily` from dynamical.org: one `time` per UTC day, `aggregation_period` `1 day` and an `aggregation_coverage` coord (share of the day's 48 half hours present). Incomplete days (the newest, or store gaps) are kept with their coverage and listed on stderr; convert-to-totals drops them by default. A cell missing half hours that another cell has that day is NaN, not a partial mean.
- Global attrs `imerg_version` (`early`, `late` or `final`), `weather_skills_source` naming the source actually used (`dynamical:nasa-imerg-analysis-early`, `dynamical:nasa-imerg-analysis-late`, or `imerg` for GES DISC), and `source`.

### Notes
- **Late fallback changes the interval.** When the dynamical.org catalog cannot be opened, or has no data in the window, `late` reads the GES DISC daily files instead and writes **daily** steps, not half-hourly. A one-line stderr note names the reason; check `weather_skills_source` or `data_interval` before combining outputs.
- Earthdata credentials come from `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`, then `~/.netrc`; they are read only for `final` and the Late fallback. Failure exits 1.
- Final lags months (its V07 collection ends 2025-09-30 as of October 2026); run `--probe-latest final` and use `final` only up to that day. A window crossing that day is two fetches, Final then Late. A `final` window with no published day exits 1.
- An `--end-time` past the latest published time writes what exists and warns on stderr. A GES DISC day missing inside the range exits 1. Missing data is never filled.
- dynamical.org reads only the `--bbox` cells. Without `--bbox` a half-hourly global day is about 1.2 GB, so always pass `--bbox`.
- GES DISC downloads a whole global file (about 32 MB) per day, one at a time, deleted once cut to `--bbox`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2025-03-01 --end-time 2025-05-31 --version final --bbox 5.5/33.5/-5/42.5 -o imerg_final_mam2025.zarr
```
Writes 92 days of IMERG Final daily rainfall rates over Kenya.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-11-01 --end-time 2026-11-02 --bbox 1/36/-1/38 -o imerg_late.zarr
```
Writes 96 half-hourly IMERG Late rates (no login) over central Kenya.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 1998-12-30 --end-time 2025-07-20 --daily --bbox 5.5/33.5/-5/42.5 -o imerg_late_daily_kenya.zarr
```
Writes daily IMERG Late rates over Kenya from 1998 to mid-2025 in one call.

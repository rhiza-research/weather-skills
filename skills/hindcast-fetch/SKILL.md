---
name: hindcast-fetch
description: "Fetch ECMWF, GEFS or Cumulus subseasonal hindcasts (reforecasts) for one or more ranges of init dates over a region, as a multi-init weather-skills forecast Zarr of daily precipitation, 2 m temperature and (ECMWF) sea-surface temperature. Use when a task needs past forecasts for calibration, bias correction, model climatologies or skill scores."
license: MIT
compatibility: "Requires Python 3.12 and uv. ECMWF and GEFS need no credentials. Cumulus needs AZURE_STORAGE_SAS_TOKEN, the same Azure read SAS as cumulus-fetch."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - tmp2m
    - sst
  openclaw:
    envVars:
      - name: AZURE_STORAGE_SAS_TOKEN
        description: Read SAS (list and read, sp=rl) for Azure container italynorthdata/data. Needed for Cumulus only.
---

# hindcast-fetch

Fetches every hindcast init in one or more date windows and stacks them on one `time` axis.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf|ecmwf_wb2|gefs|cumulus|cumulus_0p25 --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o OUT.zarr \
  [-v precip] [-v tmp2m] [-v sst] [--bbox N/W/S/E] [--members]
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET]   # latest init date, no download
```

| `--dataset` | Inits | Members | Leads (days) | Grid |
| --- | --- | --- | --- | --- |
| `ecmwf` (default choice) | every second day, 2006-01-03 to 2025-06-01, one model cycle (IFS CY49R1) | mean of 10, or 10 with `--members` | 0–45 | 1.5° |
| `ecmwf_wb2` | daily dates 1996-01-04 to 2021-12-29, mixed model cycles | mean of 10, or 10 with `--members` | 0–45 | 1.5° |
| `gefs` | Wednesdays 1998-12-30 to 2019-08-28 | 11 | 0–34 | 1° |
| `cumulus` | every second day of each year, 1996-01-01 to 2026-08-31 (`AZURE_STORAGE_SAS_TOKEN`) | 30 | 0–45 | 1° |
| `cumulus_0p25` | as `cumulus`; `precip` only | 30 | 0–45 | 0.25° |

### Arguments
- `--start-time`, `--end-time` — inclusive window of init dates. Repeat the pair for several disjoint windows (e.g. each March to May); every init in any window is kept once.
- `--variable`, `-v` — `precip` (default), `tmp2m`, and for `ecmwf` `sst`; repeatable.
- `--bbox` — `N/W/S/E`. Omit for the whole globe, which is read into memory.
- `--members` — `ecmwf` and `ecmwf_wb2` only: the 10 perturbed members instead of their mean. `gefs`, `cumulus` and `cumulus_0p25` always return every member.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, [number,] step, latitude, longitude)`: `time` is the init date; `step=0` is the 24 h `[init, init+1d)`.
- `precip` in `mm day-1`, `tmp2m` and `sst` in `degree_Celsius`.

### Notes
- Use `ecmwf` unless you need inits before 2006: `ecmwf_wb2` changes model cycle from date to date, so a trend across its inits can be a model change.
- `precip` is clipped at 0. `sst` is NaN over land.
- `cumulus` is read straight from Cumulus's own Azure archive of its ERA5-initialised Aurora hindcast: IMERG-trained `precip` and 2 m temperature at 1°, and the same `precip` at 0.25° (`cumulus_0p25`). Its 30 members are interchangeable draws; none is a control.
- If one variable's store lacks an init, that variable is NaN there (stderr lists them); nothing is filled.
- Inits fall on different days per dataset; line datasets up by valid date (`step-to-time`), not by init.
- A small `--bbox` is fast for `ecmwf`, `ecmwf_wb2` and `gefs`: a whole record over Kenya takes under a minute.
- `cumulus` is slow: its store is chunked by 100 inits with all members and 15° tiles, about 110 MB a chunk, so even one week over a small box takes over a minute and the whole 1996–2026 record over Kenya is about 13 GB per variable. `cumulus_0p25` chunks hold 5 inits instead of 100 (same tile size and chunk bytes), so a short window reads far less but the whole record over Kenya is about 16 times larger. Ask for only the init windows you need.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset ecmwf --members -v precip -v tmp2m --start-time 2006-01-01 --end-time 2025-06-01 \
  --bbox 5.5/33.5/-5/42.5 -o ecmwf_hindcast_kenya.zarr
```
Writes 10-member daily precipitation and 2 m temperature hindcasts over Kenya for every ECMWF init from 2006 to 2025.

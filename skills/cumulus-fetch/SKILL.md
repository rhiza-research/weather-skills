---
name: cumulus-fetch
description: "Fetch the Cumulus AI operational ensemble precipitation forecast (Aurora S2S run, 30 members, 46 daily leads, 0.25° or 1° global grid) for one init date or a range of inits and optional region from Cumulus's private Azure storage, as a weather-skills Zarr. Inits from 1996 to 2024 come from Cumulus's own hindcast, which also has 2 m temperature. Needs an Azure read token (AZURE_STORAGE_SAS_TOKEN). Use when a task needs the Cumulus forecast."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private Cumulus data on Azure. Requires AZURE_STORAGE_SAS_TOKEN (a read SAS with list and read, sp=rl).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.10"
  catalog-group: fetchers
  variables:
    - precip
    - t2m
  openclaw:
    requires:
      env:
        - AZURE_STORAGE_SAS_TOKEN
    primaryEnv: AZURE_STORAGE_SAS_TOKEN
    envVars:
      - name: AZURE_STORAGE_SAS_TOKEN
        description: Read SAS (list and read, sp=rl) for the Cumulus Azure storage
---

# cumulus-fetch

Fetches one Cumulus init, or every init in one or more date windows (all leads, all members), and writes daily precipitation rates. Inits before the live record come from Cumulus's hindcast automatically (see Coverage).

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--dataset precip|precip_1deg|precip_era5] [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v precip] -o OUT.zarr
# Every 00 UTC init in a window, in one call; repeat the pair for several windows
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--start-time ... --end-time ...] --bbox N/W/S/E -o OUT.zarr
# Latest init date (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET]
```

### Arguments
- `--dataset` — `precip` (IMERG-trained, 0.25°), `precip_1deg` (IMERG-trained, 1°) or `precip_era5` (ERA5-trained, 1°). Anything else exits 2. Default: `precip`.
- `--date` — init date; takes that day's latest init hour. Default: the latest published init. A date with no init exits 1 and lists the nearest published inits, so pick one of those rather than trying other dates.
- `--start-time`, `--end-time` — an init window, both days inclusive. Repeat the pair for several windows, one window per pair. Takes every 00 UTC init in the windows with all its leads: like `--date`, the windows select init times, not valid times. Use this instead of one `--date` call per init; not with `--date`. No init in any window exits 1.
- `--bbox` — `N/W/S/E` decimal degrees; negative longitudes work. Omit for the full global grid (about 5.5 GB per init at 0.25°, 350 MB at 1°).
- `--variable`, `-v` — `precip` (default; also `tp`, `precipitation`, `total_precipitation`) or `t2m` (also `2t`, `tas`, `temperature_2m`). `t2m` is in the hindcast only, on its 1° grid: it needs a `--date` or windows from 1996–2024, takes no `--dataset`, and is fetched in its own call, not with `precip`. Anything else exits 2.
- `--workers` — maximum concurrent chunk requests, at least 1. Default: 8.
- `--probe-latest` — print the latest live init date (`YYYY-MM-DD`, or `none`) on stdout and exit. Optional value: a `--dataset` id.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `precip` in `mm day-1`, dims `(number, step, latitude, longitude)`, scalar `time` = init; with windows, `(time, step, number, latitude, longitude)`, one `time` per init.
- `step` is the start of each 24 h period: `step = 0` is `[init, init + 1 day)`, up to 46 leads.
- `t2m` (`degree_Celsius`) is the 2 m temperature at `init + step`, an instant rather than a daily mean, so its steps run from 1 to 46 days.
- `number` is 0–29; members are interchangeable draws (member 0 is not a control).
- Longitude `[-180, 180)`, `data_interval` `1 day`. `weather_skills_source` is `cumulus:<dataset>` for live inits and `cumulus-hindcast:<dataset>` for hindcast inits.
- Hindcast output has the same names, dims, units and grid as live output for the same `--dataset`.

### Coverage
- Live runs: from 2026-09-01 to the latest init; `--probe-latest` gives the end.
- Hindcast: every second day from 1996-01-01 to 2024-12-31, all three precip datasets plus `t2m`, read automatically for earlier dates. A `--date` with no hindcast init exits 1 and lists the nearest ones. There are no inits between the two records.
- One call reads one record. Windows holding inits on both sides of the first live init exit 2 and name the date to split at; make one call per side.

### Notes
- Set `AZURE_STORAGE_SAS_TOKEN` (a read SAS with list and read, `sp=rl`) before the first call, including `--probe-latest`. A leading `?` or a full URL is accepted. Never print the value. When it is not set the workspace has no Cumulus access: say so and use another forecast instead of retrying.
- An init still being written can lack its last leads: those are dropped with a `WARNING` on stderr, never padded. An init with no data in the bbox exits 1; pass an earlier `--date`. With windows, a short init keeps its missing leads as NaN (not filled) with a `WARNING` naming it, and an init with no data stays all NaN.
- Window size: memory is about inits × 30 members × 46 leads × grid cells × 4 bytes, doubled while joining. A Kenya-size box at 0.25° is about 9 MB per init, so a season of daily inits is about 1 GB; keep a window under about 100 inits there, and always pass `--bbox` with windows.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -o cumulus_kenya.zarr
```
Writes the latest init's 30-member daily precipitation rates over Kenya for 46 days.
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2010-03-01 --end-time 2010-03-07 --bbox 5.5/33.5/-5/42.5 -o cumulus_kenya_2010.zarr
```
Writes the hindcast inits of the first week of March 2010 over Kenya, one `time` per init.
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py -v t2m --start-time 2010-03-01 --end-time 2010-03-07 --bbox 5.5/33.5/-5/42.5 -o cumulus_t2m_kenya_2010.zarr
```
Writes 30-member 2 m temperature hindcasts over Kenya for the same week.

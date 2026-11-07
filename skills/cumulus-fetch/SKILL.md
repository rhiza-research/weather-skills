---
name: cumulus-fetch
description: "Fetch the Cumulus AI operational ensemble precipitation forecast (Aurora S2S run, 30 members, 46 daily leads, 0.25° or 1° global grid) for one init date or a range of inits and optional region from a private Azure Blob container, as a weather-skills Zarr. Inits from 1996 to 2024 come from Cumulus's own hindcast (1°). Needs an Azure read token (AZURE_STORAGE_SAS_TOKEN). Use when a task needs the Cumulus forecast."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private Zarr stores from Azure Blob italynorthdata/data (live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9 and ml_reforecasts/gratis-czajkowski-era5) via adlfs. Requires AZURE_STORAGE_SAS_TOKEN (a read SAS with list and read, sp=rl).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.9"
  catalog-group: fetchers
  variables:
    - precip
  openclaw:
    requires:
      env:
        - AZURE_STORAGE_SAS_TOKEN
    primaryEnv: AZURE_STORAGE_SAS_TOKEN
    envVars:
      - name: AZURE_STORAGE_SAS_TOKEN
        description: Read SAS (list and read, sp=rl) for Azure container italynorthdata/data
---

# cumulus-fetch

Fetches one Cumulus init, or every init in one or more date windows (all leads, all members), and writes daily precipitation rates. Live inits come from the operational runs; earlier inits come from Cumulus's hindcast automatically (see Coverage).

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--dataset precip|precip_1deg|precip_era5] [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v precip] -o OUT.zarr
# Every 00 UTC init in a window, in one call; repeat the pair for several windows
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--start-time ... --end-time ...] --bbox N/W/S/E -o OUT.zarr
# Latest init date (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET]
```

### Arguments
- `--dataset` — `precip` (IMERG-trained, 0.25°), `precip_1deg` (IMERG-trained, 1°) or `precip_era5` (ERA5-trained). The matching store folder name (e.g. `total_precipitation_24h_acc_imerg_0p25`) also works; anything else exits 2. Default: `precip`.
- `--date` — init date; takes that day's latest init hour. Default: the latest published init. A date with no init exits 1 and lists the nearest published inits, so pick one of those rather than trying other dates.
- `--start-time`, `--end-time` — an init window, both days inclusive. Repeat the pair for several windows, one window per pair. Takes every 00 UTC init in the windows with all its leads: like `--date`, the windows select init times, not valid times. Use this instead of one `--date` call per init; not with `--date`. No init in any window exits 1.
- `--bbox` — `N/W/S/E` decimal degrees; negative longitudes work. Omit for the full global grid (about 5.5 GB per init at 0.25°, 350 MB at 1°).
- `--variable`, `-v` — each store holds one field, written as `precip`. Accepts `precip`, `tp`, `precipitation`, `total_precipitation` or a folder name; anything else exits 2. Optional.
- `--workers` — maximum concurrent chunk requests, at least 1. Default: 8.
- `--probe-latest` — print the latest live init date (`YYYY-MM-DD`, or `none`) on stdout and exit. Optional value: a `--dataset` id.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `precip` in `mm day-1`, dims `(number, step, latitude, longitude)`, scalar `time` = init; with windows, `(time, step, number, latitude, longitude)`, one `time` per init.
- `step` is the start of each 24 h period: `step = 0` is `[init, init + 1 day)`, up to 46 leads.
- `number` is 0–29; members are interchangeable draws (member 0 is not a control).
- Longitude `[-180, 180)`. `weather_skills_source=cumulus:<store folder>` (live) or `cumulus-hindcast:gratis-czajkowski-era5` (hindcast), `data_interval` `1 day`.
- Hindcast output has the same names, dims and units as live output; only the grid (1°) and the source differ. One stderr line names the source read.

### Coverage
- Live runs: from the first live init (2026-09-01) to the latest; `--probe-latest` gives the end.
- Earlier inits: Cumulus's own Aurora hindcast `gratis-czajkowski-era5`, IMERG-trained precip on a 1° grid, 30 exchangeable members, 46 daily leads, inits every second day of each year from 1996-01-01 to 2024-12-31. A `--date` with no hindcast init exits 1 and lists the nearest hindcast inits. Inits between the two records do not exist.
- `--dataset precip_1deg` reads the hindcast as is. `--dataset precip` (0.25°) on historical inits writes the 1° hindcast with a `WARNING` that the grid is 1°. `--dataset precip_era5` on historical inits exits 2: there is no ERA5-trained hindcast.
- One call reads one source. Windows holding inits on both sides of the first live init exit 2 and name the date to split at; make one call per side.

### Notes
- The container is private. Set `AZURE_STORAGE_SAS_TOKEN` (a read SAS with list and read, `sp=rl`) before the first call, including `--probe-latest`. A leading `?` or a full blob URL is accepted. Never print the value. When it is not set the workspace has no Cumulus access: say so and use another forecast instead of retrying.
- An init still being written can lack its last leads: those are dropped with a `WARNING` on stderr, never padded. An init with no data in the bbox exits 1; pass an earlier `--date`. With windows, a short init keeps its missing leads as NaN (not filled) with a `WARNING` naming it, and an init with no data stays all NaN.
- The hindcast is one Zarr of every init, stored in chunks of 100 inits × all 30 members × 46 leads × 15° tiles (about 110 MB each). Any hindcast read downloads whole chunks, so even one init over a small box takes tens of seconds (four inits over Kenya took under 2 minutes), and a box crossing a 15° tile line reads twice the data. Ask for the inits you need in one call instead of one call per init.
- Window size: each init is loaded for the bbox, then the inits are joined, so memory is about inits × 30 members × 46 leads × grid cells × 4 bytes, doubled while joining. A country-size box at 0.25° (Kenya) is about 9 MB per init, so a season of daily inits is about 1 GB; keep a window under about 100 inits there, and always pass `--bbox` with windows.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -o cumulus_kenya.zarr
```
Writes the latest init's 30-member daily precipitation rates over Kenya for 46 days.
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-03-01 --end-time 2026-05-31 --bbox 5.5/33.5/-5/42.5 -o cumulus_kenya_mam.zarr
```
Writes every 00 UTC init from March to May over Kenya, one `time` per init.

## Details

Live stores live at `az://italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/<folder>/YYYY-MM-DD-HH.zarr`, one per init. The hindcast is `az://italynorthdata/data/ml_reforecasts/gratis-czajkowski-era5/zarr/total_precipitation_24h_acc_imerg.zarr`, every init in one store. In both, each lead is the 24 h total ending at that lead; the skill relabels it to the period start and divides by one day to give a rate.

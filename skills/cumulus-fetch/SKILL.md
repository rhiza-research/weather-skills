---
name: cumulus-fetch
description: "Fetch the Cumulus AI operational ensemble precipitation forecast (Aurora S2S run, 30 members, 46 daily leads, 0.25° or 1° global grid) for one init date and optional region from a private Azure Blob container, as a weather-skills Zarr. Needs an Azure read token (AZURE_STORAGE_SAS_TOKEN). Use when a task needs the Cumulus forecast."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private Zarr stores from Azure Blob italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/total_precipitation_24h_acc_imerg_0p25 via adlfs. Requires AZURE_STORAGE_SAS_TOKEN (a read SAS with list and read, sp=rl).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.3"
  catalog-group: fetchers
  variables:
    - tp
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

Fetches one Cumulus init (all leads, all members) and writes daily precipitation rates.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--dataset precip|precip_1deg|precip_era5] [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v tp] -o OUT.zarr
# Latest init date (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET]
```

### Arguments
- `--dataset` — `precip` (IMERG-trained, 0.25°), `precip_1deg` (IMERG-trained, 1°) or `precip_era5` (ERA5-trained). The matching store folder name (e.g. `total_precipitation_24h_acc_imerg_0p25`) also works; anything else exits 2. Default: `precip`.
- `--date` — init date; takes that day's latest init hour. Default: the latest published init.
- `--bbox` — `N/W/S/E` decimal degrees; negative longitudes work. Omit for the full global grid (about 5.5 GB per init at 0.25°, 350 MB at 1°).
- `--variable`, `-v` — each store holds one field, written as `tp`. Accepts `tp`, `precip`, `precipitation`, `total_precipitation` or a folder name; anything else exits 2. Optional.
- `--workers` — maximum concurrent chunk requests, at least 1. Default: 8.
- `--probe-latest` — print the latest init date (`YYYY-MM-DD`, or `none`) on stdout and exit. Optional value: a `--dataset` id.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `tp` in `mm day-1`, dims `(number, step, latitude, longitude)`, scalar `time` = init.
- `step` is the start of each 24 h period: `step = 0` is `[init, init + 1 day)`, up to 46 leads.
- `number` is 0–29; members are interchangeable draws (member 0 is not a control).
- Longitude `[-180, 180)`. `weather_skills_source=cumulus:<store folder>`, `data_interval` `1 day`.

### Notes
- The container is private. Set `AZURE_STORAGE_SAS_TOKEN` (a read SAS with list and read, `sp=rl`) before the first call, including `--probe-latest`. A leading `?` or a full blob URL is accepted. Never print the value.
- An init still being written can lack its last leads: those are dropped with a `WARNING` on stderr, never padded. An init with no data in the bbox exits 1; pass an earlier `--date`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -o cumulus_kenya.zarr
```
Writes the latest init's 30-member daily precipitation rates over Kenya for 46 days.

## Details

Stores live at `az://italynorthdata/data/live_forecasts/global_model/aurora_s2s/utmost-plane-16dd148fe73d4cbb9/zarr/<folder>/YYYY-MM-DD-HH.zarr`, one per init. Each lead in a store is the 24 h total ending at that lead; the skill relabels it to the period start and divides by one day to give a rate.

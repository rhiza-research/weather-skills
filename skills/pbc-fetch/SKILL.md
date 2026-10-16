---
name: pbc-fetch
description: "Fetch a StillLearning PBC (probabilistic bias correction) AI Weather Quest precipitation forecast for week 3 or week 4 from the public Sheerwater datalake, as a weather-skills Zarr. Values are the probability of each rainfall quintile on a 1.5° global grid, not amounts. Use when a task needs PBC subseasonal precipitation probabilities."
license: MIT
compatibility: "Requires Python 3.12 and uv. Reads the public GCS bucket gs://sheerwater-public-datalake/pbc-data anonymously via gcsfs."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - pr
---

# pbc-fetch

Fetches one PBC init's weekly precipitation quintile probabilities.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--dataset ID] [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v pr] -o OUT.zarr
# Latest init date (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [ID]
```

| `--dataset` | Aliases | Valid week (day 1 = init) | `step` |
|---|---|---|---|
| `era5-p_pr_19` | `pr_19`, `p1` | days 19–25 (week 3) | 18 days |
| `era5-p_pr_26` | `pr_26`, `p2` | days 26–32 (week 4) | 25 days |

### Arguments
- `--dataset` — product id or alias from the table. Default: `era5-p_pr_19`.
- `--date` — forecast init date. A date with no published store exits 1. Default: the latest published init.
- `--bbox` — `N/W/S/E` decimal degrees. Omit for the full global grid.
- `--variable`, `-v` — the only variable is `pr` (aliases `forecast`, `precip`, `tp`, `probability`). Optional.
- `--probe-latest` — print the latest init date (`YYYY-MM-DD`) on stdout and exit. Optional value: a `--dataset` id or alias.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `pr`, units `1`, dims `(quintile, step, latitude, longitude)`, scalar `time` = init, and `valid_time` on `step` (first day of the valid week).
- `quintile` is `0.2, 0.4, 0.6, 0.8, 1.0`, the upper bound of each quintile; the five probabilities sum to 1. Cells with no signal hold 0.2; there are no NaNs.
- One `step`. Global 1.5° grid (the ECMWF 1.5° points), longitude `[-180, 180)`.
- `data_interval` `7 day`. Global attrs `weather_skills_source=sheerwater-pbc:<dataset>`, `pbc_dataset`, `pbc_folder`.

### Notes
- Values are probabilities, not amounts: do not convert them to mm or totals.
- To map one category, select a single `quintile` value first.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset era5-p_pr_19 --bbox 5.5/33.5/-5/42.5 -o pbc_week3_kenya.zarr
```
Writes the latest init's week-3 quintile probabilities over Kenya.

## Details

Stores are at `gs://sheerwater-public-datalake/pbc-data/<dataset>/YYYYMMDD/<dataset>-YYYYMMDD.zarr`, where the folder date is the first valid day: init + 18 days for week 3, init + 25 days for week 4. The skill converts `--date` to that folder date.

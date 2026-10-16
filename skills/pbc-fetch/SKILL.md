---
name: pbc-fetch
description: Fetch a PBC (probabilistic bias correction) AI Weather Quest precipitation forecast from gs://sheerwater-public-datalake/pbc-data and write a weather-skills standard dataset Zarr. Quintile probabilities on the 1.5° AI-WQ grid (`pr`, units 1, dim `quintile` 0.2/0.4/0.6/0.8/1.0). `--dataset era5-p_pr_19` (aliases `pr_19`, `p1`) is week 3, days 19–25; `era5-p_pr_26` (`pr_26`, `p2`) is week 4, days 26–32. Use when a task needs StillLearning / PBC subseasonal precip probabilities from the Sheerwater datalake — not dynamical-fetch or ecmwf-fetch. Public bucket; no credentials needed.
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

Opens a StillLearning PBC (probabilistic bias correction) precipitation
forecast from `gs://sheerwater-public-datalake/pbc-data`, maps it onto a classic
weather-skills forecast, and writes a local Zarr. The values are **quintile
probabilities** (they sum to 1 across `quintile`), not millimetres.

Layout:

```
era5-p_pr_19/YYYYMMDD/era5-p_pr_19-YYYYMMDD.zarr/   # week 3, days 19–25
era5-p_pr_26/YYYYMMDD/era5-p_pr_26-YYYYMMDD.zarr/   # week 4, days 26–32
```

Store folders are named by the **first day of the valid week**. `--date` is
the forecast **init** (same convention as `ecmwf-fetch` / `kenya-forecast-fetch`):
week 3 lives at `init + 18d`, week 4 at `init + 25d`. An init with no folder
is an error — `--date` is never read as a folder name (for folder `YYYYMMDD`
pass that date minus 18 / 25 days). Default `--date` is the latest folder that
has a Zarr.

## When to use

- A task needs PBC / StillLearning AI Weather Quest precip probabilities
  already synced to the Sheerwater datalake (no AI-WQ portal, no ECDS queue).
- Downstream `select --dim quintile` or `clip-region`.

Prefer `dynamical-fetch` / `ecmwf-fetch` for physical precip rates. This skill
does **not** convert probabilities to `mm` — do not run `deaccumulate` or
`convert-to-totals` on the output.

## Access

`gs://sheerwater-public-datalake/pbc-data` is a public mirror of the
Sheerwater PBC stores, read anonymously: no credentials or env vars.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> \
    [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v pr] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [dataset-id]
```

### Datasets

| `--dataset` | Aliases | Valid week | `step` | Folder = init + |
|---|---|---|---|---|
| `era5-p_pr_19` (default) | `pr_19`, `p1` | days 19–25 | 18 days | 18 d |
| `era5-p_pr_26` | `pr_26`, `p2` | days 26–32 | 25 days | 25 d |

Output dims: `(quintile, step, latitude, longitude)` with scalar `time` (init).
`quintile` is `0.2, 0.4, 0.6, 0.8, 1.0` (AI-WQ upper bounds). Grid is global
1.5° (latitude 90 → -90, same points as ECMWF 1.5°), longitude wrapped to
`[-180, 180)`. `pr` has `units="1"` and no `standard_name` (CF has none for
it); the store has no NaNs (uninformative cells hold 0.2). Native period
is one week (`data_interval` `7 day`). To map a single quintile, first
`select --dim quintile --value 0.2` (or `0.4` / `0.6` / `0.8` / `1.0`).

### Arguments

- `--dataset` — product id from the table (default `era5-p_pr_19`).
- `--date` — optional init date `YYYY-MM-DD`. Default: latest folder with a
  Zarr. Calendar day: `resolve-time latest`. Latest published init:
  `--probe-latest`. A missing init exits with an error.
- `--probe-latest [dataset-id]` — print the latest init `YYYY-MM-DD` on stdout
  and exit. No `-o`.
- `--bbox` — optional spatial subset `N/W/S/E`. Country boxes: `resolve-region`.
- `--variable`, `-v` — restrict to named data variables. Only `pr` (aliases
  `forecast`, `precip`, `tp`, `probability`). Omit to fetch `pr`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated weather-skills standard dataset Zarr. Classic forecast shape:
scalar `time` (init) + size-1 `step` (lead to the first day of the valid week)
+ `quintile` + `latitude`/`longitude`. Stamped with
`weather_skills_source=sheerwater-pbc:<dataset>`.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# Latest week-3 PBC precip probabilities, Kenya
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset era5-p_pr_19 \
    --bbox 5/34/-5/42 -o /tmp/pbc_pr19.zarr

# Pin an init (Thursday AI-WQ schedule; some recent runs are daily)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset pr_26 --date 2026-08-24 \
    --bbox 5/34/-5/42 -o /tmp/pbc_pr26.zarr
```

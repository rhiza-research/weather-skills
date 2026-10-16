---
name: kenya-forecast-fetch
description: "Fetch a published forecast grid from the public KMSA / Rhiza Kenya forecasts archive (gs://kenya-forecasting-data) for one init date, as a weather-skills Zarr. Products: native ECMWF S2S precip (~1.5°, 101 members), its ~0.05° CHIRPS-resolution weekly downscale (101 members) and daily downscale (ensemble mean), S2S temperatures, humidity and winds, medium-range precip, and GEFS. No credentials needed."
license: MIT
compatibility: Requires Python 3.12 and uv. Opens public consolidated Zarr (native S2S / GEFS / medium-range), the weekly downscaled NetCDF, or the daily downscaled GeoTIFF over HTTPS from Google Cloud Storage bucket kenya-forecasting-data; no credentials required. Older init folders may only have GRIB/NetCDF under data/ and no Zarr.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - d2m
    - cape
    - tcw
---

# kenya-forecast-fetch

Opens one product from an init folder `gs://kenya-forecasting-data/YYYY-MM-DD/data/`, subsets it, and writes a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--dataset ID] [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v VAR ...] -o OUT.zarr
# Latest init that has the product (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [ID]
```

**Dataset ids** (variables; members on `number`):
- `precip` — native S2S `tp`, daily, ~1.5°, 101 members.
- `precip_downscaled` — `tp`, weekly, ~0.05° over 33–42°E 5°S–6°N, 101 members (inits from 2026-09-07; earlier inits have one field).
- `precip_downscaled_daily` — `tp`, daily, ~0.05° over 27–43°E 7.5°S–7.5°N, ensemble mean only.
- `daily_vars` — `t2m`, `d2m`, `cape`, `tcw` daily means. `Tminmax` — `mn2t6`, `mx2t6`.
- `10wind` — `u10`, `v10`. `500wind` — `w`. `700wind` — `u`, `v`.
- `medium_range_precip` — `tp`, weekly. `gefs` — `tp`, daily.

### Arguments
- `--dataset` — id from the list. Default: `precip`.
- `--date` — init date. Default: the latest init folder that has the product.
- `--bbox` — `N/W/S/E`; keeps cell centres inside the box. Omit for the full grid.
- `--variable`, `-v` — keep only these data variables; repeatable. An unknown name exits and lists `Available:`.
- `--probe-latest [ID]` — print the latest init date for `ID` (default: `--dataset`) and exit.
- `--output`, `-o` — output Zarr.

### Output
- Dims `(step, [number,] latitude, longitude)` with scalar `time` = init. Longitude is `[-180, 180)`.
- Precip is a rate in `mm day-1`; air temperature in `degree_Celsius`; `tcw` (total column water) stays `kg m-2`.
- Precip, `gefs`, `daily_vars` and `medium_range_precip` steps are interval starts: `step = 0` is `[init, init + 1 period)`. Winds and `Tminmax` keep the archive steps (`Tminmax`: 6-hour min/max ending at each step, every 12 h from 6 h).
- `precip_downscaled` and `medium_range_precip` carry `aggregation_period` (one week), so they are ready for period totals without weekly aggregation.

### Notes
- Lead counts vary by init: recent weekly files have 4 weeks (the daily GeoTIFF 28 days), older ones 6 weeks.
- The downscaled NetCDF and GeoTIFF are downloaded whole before `--bbox` applies (~0.7 GB peak memory for the full 101-member weekly file).
- Downscaled cell centres can sit half a cell off CHIRPS; align grids before a cell-by-cell comparison.
- A `--date` folder without the product exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip_downscaled --date 2026-10-12 \
  --bbox 5.5/33.5/-5/42.5 -o kenya_tp_downscaled.zarr
```
Writes 101-member weekly precip rates at ~0.05° over Kenya for the 2026-10-12 init.

## Details

**Choosing a product for probabilities.** Daily-window events (dry spells, `>50 mm in one day`) need daily members: use `precip`. The daily downscale is a mean, so it gives yes/no maps only. Weekly-total events and spread (share of members under 10 mm in week 2, member std) can use `precip_downscaled` at ~0.05°. Keep `number` until the final reduction.

The archive is browsable at https://kenya-forecasts.sheerwater.rhizaresearch.org/files/. Full pipelines that rebuild the KMSA weekly figures: [references/EXAMPLES.md](references/EXAMPLES.md).

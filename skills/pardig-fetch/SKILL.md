---
name: pardig-fetch
description: "Fetch Google PARDIG AI precipitation estimates (instantaneous rates every 15 minutes, global 0.1° grid) for a date range and lat/lon box from Google's public gs://pardig_1 time-series Zarr stores, as a weather-skills Zarr: PARDIG-NRT (2016 to 2025) or PARDIG-LR (1999 to 2023). No credentials needed. Use when a task needs sub-hourly or long gridded precipitation history at a point or over a region."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public PARDIG Zarr v3 stores on Google Cloud Storage (gs://pardig_1) anonymously via gcsfs; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
---

# pardig-fetch

Fetches PARDIG (Precipitation AI Reanalysis, Densely Inferred GPM), an experimental Google DeepMind / Google Research precipitation estimate trained on GPM CORRA, for a box and date range, as a weather-skills standard dataset Zarr.

| `--product` | Store | Period | Inputs |
|---|---|---|---|
| `nrt` (default) | `gs://pardig_1/NRT/zarr_rechunked_as_timeseries/2016_to_2025/predictions.zarr` | 2016-01-01T07:45 – 2025-12-31 | ECMWF HRES + geostationary satellites |
| `lr` | `gs://pardig_1/LR/zarr_rechunked_as_timeseries/1999_to_2023/predictions.zarr` | 1999 – 2023 | ERA5 + GridSat-B1 + microwave sounders |

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD --bbox N/W/S/E [--product nrt|lr] -o OUT.zarr
```

### Arguments
- `--start-time`, `--end-time` — inclusive range of whole UTC days.
- `--bbox` — required `N/W/S/E` decimal degrees; longitudes −180..180 or 0..360, W > E crosses the antimeridian.
- `--product` — `nrt` (default) or `lr`.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, every 15 minutes (:00, :15, :30, :45 UTC), 0.1° cells. Latitude ascending; longitude `[-180, 180)`.
- `precip` in `mm day-1` (converted from `m h-1`), `data_interval="15 minute"`. Each value is the instantaneous rate at its `time`, treated as the rate for that 15-minute slot.
- `weather_skills_source="pardig-nrt"` or `"pardig-lr"`.

### Notes
- Days outside the product's period are absent rows, not filled, with a `WARNING` on stderr; a window with no data exits 1. 2026 onward is not in these stores.
- Each chunk is one 0.5° × 0.5° tile holding the product's whole time axis (~1.5–4 MB NRT, ~3–10 MB LR), so cost scales with the box area, not the window length. A Kenya-sized box (10° × 9°, ~400 tiles) reads about 1–4 GB and takes under a minute; a year costs the same as a day. Fetch a long window in one call rather than several short ones.
- NRT and LR use different inputs; joining them can introduce a non-climatic shift. Rates are capped at 75 mm h⁻¹ and values poleward of about ±67° are extrapolated.
- Experimental data under CC BY 4.0, not validated for real-world use.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2020-04-01 --end-time 2020-04-30 \
  --bbox 5.5/33.5/-5/42.5 -o pardig_kenya_apr2020.zarr
```
Writes 2,880 15-minute precip-rate maps (mm day-1) over Kenya for April 2020.

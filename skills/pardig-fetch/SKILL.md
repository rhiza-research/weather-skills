---
name: pardig-fetch
description: "Fetch Google PARDIG AI precipitation estimates (instantaneous rates every 15 minutes, global 0.1° grid) for a date range and lat/lon box from Google's public gs://pardig_1 Zarr stores, as a weather-skills Zarr: PARDIG-NRT (2016 to a few hours ago) or PARDIG-LR (1999 to 2023). No credentials needed. Use when a task needs recent or long gridded precipitation estimates, sub-hourly, at a point or over a region."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the public PARDIG Zarr v3 stores and live frames on Google Cloud Storage (gs://pardig_1) anonymously via gcsfs; no credentials required.
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
| `nrt` (default) | `gs://pardig_1/NRT/zarr_rechunked_as_timeseries/2016_to_2025/predictions.zarr`, then from 2026 the live frames `gs://pardig_1/NRT/zarr/2026_to_present/<YYYYMMDDTHHMMSS>/predictions.zarr` | 2016-01-01T07:45 – a few hours ago | ECMWF HRES + geostationary satellites |
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
- Frames the source lacks (outside the product's period, or not yet published) are absent rows, not filled; stderr warns with the count found. A window with no data exits 1.
- Live NRT frames arrive in 6-hour batches about 2–8 h behind real time, so today's window is partial.
- Before 2026 each chunk is one 0.5° × 0.5° tile holding the product's whole time axis (~1.5–4 MB NRT, ~3–10 MB LR), so cost scales with the box area, not the window length. A Kenya-sized box (10° × 9°, ~400 tiles) reads about 1–4 GB and takes under a minute; a year costs the same as a day.
- From 2026 each live frame is one global chunk (~1.2 MB), so cost scales with the window, not the box: a week (672 frames) reads ~0.8 GB in about 40 s for any box. Keep live windows to weeks.
- gcsfs may print a `RuntimeError` traceback while closing its connection at exit; it comes after `Wrote:`, the exit code stays 0 and the output is complete.
- NRT and LR use different inputs; joining them can introduce a non-climatic shift. Rates are capped at 75 mm h⁻¹ and values poleward of about ±67° are extrapolated.
- Experimental data under CC BY 4.0, not validated for real-world use.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-11-02 --end-time 2026-11-08 \
  --bbox 5.5/33.5/-5/42.5 -o pardig_kenya_week.zarr
```
Writes 672 15-minute precip-rate maps (mm day-1) over Kenya for one week.

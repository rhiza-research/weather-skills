---
name: oisst-fetch
description: "Fetch NOAA OISST v2.1 daily 0.25° sea-surface temperature (1981-09 to present) for a date range and region from NOAA PSL's public OPeNDAP server, as a weather-skills Zarr. No credentials needed. Use when a task needs gridded SST observations, e.g. for ocean analysis or comparison against forecasts or reanalysis."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads NOAA OISST v2.1 from NOAA PSL's OPeNDAP server (psl.noaa.gov) over HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
  catalog-group: fetchers
  variables:
    - sst
---

# oisst-fetch

Reads the requested days and box of NOAA's OISST v2.1 daily analysis over OPeNDAP (a protocol that transfers only the requested slice) and writes them as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--bbox N/W/S/E] -o OUT.zarr
# Latest published day (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range.
- `--bbox` — `N/W/S/E` decimal degrees, either longitude convention; W > E crosses the antimeridian. Omit for the full globe.
- `--probe-latest` — print the latest published day (`YYYY-MM-DD`) and exit. Takes no id; passing one exits 2.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, daily, 0.25° cell centres (…, -0.125, 0.125, …). Longitude is `[-180, 180)`, ascending.
- `sst` in `degree_Celsius`, `standard_name=sea_surface_temperature`, `data_interval="1 day"`. Land cells are NaN.
- `weather_skills_source="oisst"`.

### Notes
- Days less than about 15 days old are preliminary and may be revised; nothing in the output marks them. A window ending in that span prints a stderr note.
- Days outside the published record are left out, not filled, and stderr reports how many are missing. A window with no data exits 1.
- The server limits request size. A too-large request exits 2 asking for a smaller `--bbox` or shorter window. A transient server error is retried once, then exits 1; retrying later may work.
- The whole selection is held in memory: about 4 MB per global day.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-09-01 --end-time 2026-09-30 \
  --bbox 10/40/-15/60 -o oisst_w_indian_ocean.zarr
```
Writes 30 daily SST maps (°C) for the western Indian Ocean off East Africa.

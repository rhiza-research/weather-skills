---
name: 3dpaws-fetch
description: "Fetch daily 3D-PAWS (3D-Printed Automatic Weather Station) observations (rainfall, mean, max and min temperature, humidity, pressure, wind speed and gust) from a CHORDS portal, by default the FEWS NET network of Kenya Met Department and KALRO stations, for chosen station ids or every station in a lat/lon box, as a weather-skills station Zarr. Can also list the stations. Needs a CHORDS email and API key. Use when a task needs 3D-PAWS station data, for example to compare against gridded satellite or forecast data."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires CHORDS_EMAIL and CHORDS_API_KEY in the environment for downloads (not for --list-stations).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - temperature
    - tmax
    - tmin
    - humidity
    - pressure
    - wind_speed
    - wind_gust
  openclaw:
    requires:
      env:
        - CHORDS_EMAIL
        - CHORDS_API_KEY
    primaryEnv: CHORDS_API_KEY
---

# 3dpaws-fetch

Lists the 3D-PAWS stations on a CHORDS portal, or downloads their raw readings and writes daily values as one station Zarr.

## Usage

```bash
# List stations (TSV on stdout, no -o); --bbox optional
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations [--bbox N/W/S/E] [--portal URL]
# Fetch chosen stations, or every station in a box
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py (--station ID [--station ID ...] | --bbox N/W/S/E) --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--day-start-hour H] [--portal URL] [--workers N] -o OUT.zarr
# Print the latest ended day, and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--station ID | --bbox N/W/S/E]
```

### Arguments
- `--list-stations` — print stations as TSV (`station_id`, `name`, `site`, `latitude`, `longitude`) and exit; `--bbox` filters. No credentials needed.
- `--station` — CHORDS instrument id, e.g. `4`; repeatable; overrides `--bbox`.
- `--bbox` — `N/W/S/E`; every station whose site is inside.
- `--start-time`, `--end-time` — inclusive days, `YYYY-MM-DD`.
- `--day-start-hour` — UTC hour each day starts, 0-23. Default 0. Use 6 for the KMD 06 UTC rain day.
- `--portal` — CHORDS portal URL. Default `https://3d-fewsnet.icdp.ucar.edu` (FEWS NET). Each portal needs its own key.
- `--workers` — parallel station downloads. Default 8.
- `--probe-latest` — print the last day that ended before the newest reading, and exit. Does not check the day is complete.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)`; coords `latitude`, `longitude`, `name`, `site`. `time` is every requested day, labelled by its start date.
- Whichever the stations report: `precip` (`mm day-1`), `temperature` / `tmax` / `tmin` (`degree_Celsius`), `humidity` (fraction), `pressure` (station pressure, `hPa`), `wind_speed` (mean, `m s-1`), `wind_gust` (max, `m s-1`).

### Notes
- A day is NaN unless the station sent every expected reading that day. Missing data is never filled, so a multi-day total over a station with a NaN day is NaN. stderr reports complete days per station and per variable.
- Needs `CHORDS_EMAIL` and `CHORDS_API_KEY` (from the portal's `/users` page, with download access); missing or rejected exits 2. A station that fails to download is dropped with a stderr line.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 5.5/33.5/-5/42.5
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station 1 --station 4 --start-time 2026-10-01 --end-time 2026-10-17 --day-start-hour 6 -o paws_kenya.zarr
```

## Details

- **Complete days.** The reporting interval is the station's most common spacing (per day, so a change of interval is followed). A day is complete for a variable when no two valid readings are more than 1.5 intervals apart. Rain gauges may skip up to 3 intervals, because each reading holds the rain since the last one.
- **Quality control.** Out-of-range readings (including `-999.9`) are dropped, as is an exact `0` for temperature, humidity or pressure (no sensor fitted) and readings flagged as test data.
- **Two rain gauges.** `precip` is gauge 1 where gauge 2 also has a complete day and the totals agree within 1 mm or 25 %; otherwise NaN. A station with one gauge uses it.
- 1-minute stations often skip readings, so many of their days are NaN. A few test instruments sit in Boulder; a bbox excludes them.

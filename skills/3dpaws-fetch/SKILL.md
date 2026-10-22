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
- `--list-stations` — print the portal's stations as TSV (`station_id`, `name`, `site`, `latitude`, `longitude`; blank when the site has no coordinates) and exit; with `--bbox`, only those inside it. Needs no credentials.
- `--station` — CHORDS instrument id such as `4`; repeatable. Overrides `--bbox`. An unknown id exits 2; one with no site coordinates exits 1.
- `--bbox` — `N/W/S/E` decimal degrees; without `--station`, every station whose site is inside. W > E crosses the antimeridian.
- `--start-time`, `--end-time` — inclusive range of days, `YYYY-MM-DD`.
- `--day-start-hour` — UTC hour each day starts at, 0-23. Default: 0. Day D runs D HH:00 to D+1 HH:00 UTC. Pass 6 for the KMD 06 UTC rainfall day, when most Kenyan stations reset their rain total.
- `--portal` — CHORDS portal base URL. Default: `https://3d-fewsnet.icdp.ucar.edu` (FEWS NET). Others, e.g. `https://3d-kenya.chordsrt.com`, need their own key.
- `--workers` — parallel station downloads. Default: 8.
- `--probe-latest` — print the latest day that ended before the newest reading of the `--station` / `--bbox` stations (default: all located) and exit. It does not check the day is complete. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)`; coords `latitude`, `longitude`, `name`, `site` on `station_id`. `featureType="timeSeries"`, `data_interval = "1 day"`.
- `precip` (`mm day-1`, daily sum of the rain gauge), `temperature` / `tmax` / `tmin` (`degree_Celsius`, daily mean / max / min of `mt1`), `humidity` (fraction, units `1`, daily mean of `sh1`), `pressure` (`hPa`, station pressure, mean of `bp1`), `wind_speed` (`m s-1`, mean of `ws`), `wind_gust` (`m s-1`, max of `wg`) — whichever the stations report.
- `time` holds every requested day, labelled by its start date. Stations with no complete day are left out.

### Notes
- A daily value needs every expected reading at the station's reporting interval; one missing or failed reading makes that day NaN for that variable ([Details](#details)).
- Never fill or extrapolate missing days: a multi-day total for a station missing any day is NaN; report how many stations were complete. stderr prints each station's interval and complete days, and per variable the stations covering the whole range (`precip: 2 of 4 stations complete`).
- Set `CHORDS_EMAIL` and `CHORDS_API_KEY` for downloads and `--probe-latest`; missing or rejected, it exits 2. A failed station download is dropped with a stderr line; no data at all exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 5.5/33.5/-5/42.5
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station 1 --station 4 --start-time 2026-10-01 --end-time 2026-10-17 --day-start-hour 6 -o paws_kenya.zarr
```
Lists the Kenyan stations, then writes daily values for 1-17 October 2026 at Nairobi KMD HQ and Kitui on the KMD 06 UTC day.

## Details

### Complete days
- Each station's reporting interval is the modal spacing of its readings in whole minutes, worked out per day so a station whose interval changes is judged by the new one. 1-minute stations run on their own timer, so readings arrive 60-61 s apart with drifting seconds; 15-minute stations report a few seconds after each quarter hour.
- A day whose spacing is a whole multiple of the station's usual interval counts as dropped readings, judged at the usual interval: a station losing every other reading is not read as one reporting at twice the interval. The longer spacing counts as a new interval only when the station moves from its own timer to reporting on the clock at it.
- A day is complete for a variable when no two consecutive valid readings are more than 1.5 intervals apart, which allows clock jitter but not one missing reading. Readings an hour either side of the range are fetched so the first and last days are judged the same way. A gap blanks each day its missing readings fall in. A reading sent twice counts once.
- This holds for every variable: a partial day is not 0 mm or a scaled-up sum, and a partial-day mean, maximum or minimum may miss the afternoon peak or the night low. Each variable is judged on its own valid readings; `tmax` and `tmin` share `temperature`'s.
- 1-minute stations often take about two minutes between some readings, so many of their days come out NaN.

### Quality control
- Readings outside a plausible range are missing, which drops the `-999.9` a station sends for a failed read. An exact `0` for temperature, humidity or pressure is missing too: stations without that sensor report 0. Readings flagged as test data are dropped.
- Stations carry two rain gauges. `precip` is gauge 1 (`rg1`, or `rg` on older stations) only where gauge 2 (`rg2`) also has a complete day and the two daily totals agree within 1 mm or 25 %, whichever is larger. Otherwise the day is NaN, since one gauge is faulty and there is no telling which. A station with only one gauge uses it as is.

### Portal
- Keys are issued per portal on its `/users` page, and the account needs download access.
- Site coordinates come from the portal and are not checked; a few test or draft instruments sit at the NCAR site in Boulder, which a bbox excludes.

---
name: meteosat-fetch
description: Fetch a few Meteosat geostationary scans (SEVIRI or FCI) over Africa/Europe from the EUMETSAT Data Store and write a weather-skills standard dataset Zarr of visible reflectance (%) and infrared brightness temperature (K) on a regular lat/lon grid at native sampling. Pick bands with --band vis, --band ir, or explicit names; pick views with --date plus one or more --time HH:MM (UTC). --service iodc gives the Indian Ocean satellite (45.5E, better view of East Africa). Requires EUMETSAT Data Store credentials. Use for satellite imagery snapshots (cloud tops, convection, fog); not for long time series.
license: MIT
compatibility: Requires Python 3.12 and uv. Requires EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET in the environment, or an eumdac credentials file at ~/.eumdac/credentials.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - EUMETSAT_CONSUMER_KEY
        - EUMETSAT_CONSUMER_SECRET
    primaryEnv: EUMETSAT_CONSUMER_KEY
---

# meteosat-fetch

Downloads Level 1.5 SEVIRI or Level 1c FCI scans from the EUMETSAT Data Store
(`eumdac`), calibrates them with satpy, keeps only the `--bbox`, and writes one
variable per requested band on a regular lat/lon grid. Raw files go to a
temporary directory and are deleted after each scan, so whole disks are never
stored.

## When to use

- A handful of satellite views of a region: cloud cover, convective cloud
  tops (cold IR brightness temperatures), fog, dust, smoke.
- Comparing imagery against station, radar, or precipitation products for a
  specific event time.

Not for long time series. Each scan is a large download (about 180 MB for
SEVIRI; for FCI, only the strips covering the bbox, about 200-300 MB for a
Kenya-sized region), so the skill warns above 4 scans and refuses above
`--max-scans` (default 12).

## Satellites and instruments

| `--service` | `--instrument` | Collection | Satellite | Repeat | Native sampling |
|---|---|---|---|---|---|
| `0deg` | `fci` (default from 2024-09-24) | `EO:EUM:DAT:0662` | MTG-I1 (Meteosat-12) | 10 min | 1 km VIS/NIR, 2 km IR |
| `0deg` | `seviri` (default before 2024-09-24) | `EO:EUM:DAT:MSG:HRSEVIRI` | MSG (Meteosat-11/10) | 15 min | 3 km |
| `iodc` | `seviri` (only option) | `EO:EUM:DAT:MSG:HRSEVIRI-IODC` | MSG (Meteosat-9) at 45.5E | 15 min | 3 km |

Use `iodc` for East Africa and the Horn: at 45.5E it sees the region at a much
smaller view angle than the 0deg satellite. There is no FCI IODC service.

## Bands

`--band` is repeatable and accepts comma-separated values: the shortcuts `vis`
and `ir`, or explicit names (case-insensitive). Names differ per instrument.

- SEVIRI `vis`: `VIS006`, `VIS008`, `IR_016`
- SEVIRI `ir`: `IR_039`, `WV_062`, `WV_073`, `IR_087`, `IR_097`, `IR_108`, `IR_120`, `IR_134`
- FCI `vis`: `vis_04`, `vis_05`, `vis_06`, `vis_08`, `vis_09`, `nir_13`, `nir_16`, `nir_22`
- FCI `ir`: `ir_38`, `wv_63`, `wv_73`, `ir_87`, `ir_97`, `ir_105`, `ir_123`, `ir_133`

`vis` means solar-reflective channels, calibrated to reflectance in `%`
(`toa_bidirectional_reflectance`). `ir` means thermal channels including water
vapour, calibrated to brightness temperature in `K`
(`toa_brightness_temperature`). Visible channels are meaningless at night.
SEVIRI HRV is not offered.

## Authentication

Get a consumer key and secret from the EUMETSAT API key page
(https://api.eumetsat.int/api-key/) with a free EUMETSAT account. Then either:

- set `EUMETSAT_CONSUMER_KEY` and `EUMETSAT_CONSUMER_SECRET` (checked first), or
- run `eumdac set-credentials KEY SECRET` once, which writes `~/.eumdac/credentials`.

Never pass the key or secret on the command line.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --date YYYY-MM-DD --time HH:MM [--time HH:MM ...] \
  --bbox N/W/S/E --band {vis,ir,NAME}[,...] \
  [--service {0deg,iodc}] [--instrument {seviri,fci}] -o <path.zarr>
```

### Arguments

- `--date` — UTC day of the scans.
- `--time` — UTC time, repeatable or comma-separated. Each time is floored to
  the start of its repeat cycle (15 min SEVIRI, 10 min FCI), and times that fall
  in the same cycle are fetched once.
- `--bbox` — required `N/W/S/E`. Use `resolve-region` for a country or named region.
- `--band` — see Bands. Only the requested bands are written.
- `--service` — `0deg` (default) or `iodc`.
- `--instrument` — override the default described in the table above.
- `--max-scans` — hard cap on scans per request (default 12).
- `--workers` — concurrent file downloads per scan (default 4).
- `--probe-latest` — print the latest available scan as `YYYY-MM-DD HH:MM` for
  the chosen `--service`/`--instrument` and exit. No `-o`.
- `--output`, `-o` — output Zarr path.

### Output

Zarr with dims `(time, latitude, longitude)`, one variable per band, named as
in the Bands lists. The grid is regular lat/lon at the finest requested band's
native sampling (0.01 deg for FCI VIS/NIR, 0.02 deg for FCI IR only, 0.03 deg
for SEVIRI), snapped outward to whole cells, latitude descending. The
geostationary pixels are nearest-neighbour resampled onto it, and pixels off
the disk are NaN. `time` is the nominal repeat-cycle start; the `scan_start`
coordinate holds the actual sensing start. The scan sweeps the disk over about
10-12 minutes, so a given region is imaged a few minutes after either time.

To change the resolution afterwards, run `coarsen` or `downscale` on the output.

### Provenance

Standard `weather_skills_history` stamp via the decorator, plus
`weather_skills_source=eumetsat-datastore:<collection id>`, `platform`
(e.g. `MTI1`, `MSG2`), `instrument`, `eumetsat_service`, and
`sub_satellite_longitude`.

## Example

```bash
# Latest FCI scan time, then 10.5 um IR and 0.6 um VIS over Kenya.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --date 2026-10-06 --time 12:00 \
  --bbox 5.506/33.894/-4.677/41.855 --band vis_06,ir_105 -o /tmp/fci_kenya.zarr

# All SEVIRI IR channels from the Indian Ocean satellite, two views an hour apart.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --date 2026-10-06 --time 12:00 --time 13:00 \
  --bbox 5.506/33.894/-4.677/41.855 --band ir --service iodc -o /tmp/iodc_kenya.zarr
```

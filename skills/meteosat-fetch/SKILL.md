---
name: meteosat-fetch
description: "Fetch Meteosat geostationary satellite imagery (FCI or SEVIRI) over a lat/lon box at chosen times on one day from the EUMETSAT Data Store, as a weather-skills Zarr of visible reflectance (%) and infrared brightness temperature (K) on a regular lat/lon grid. Covers Africa, Europe and the Indian Ocean every 10-15 minutes. Needs EUMETSAT credentials. Use for snapshots of cloud tops, convection, fog or dust: each scan is a 200-300 MB download, so calls are capped at 12 scans by default and long time series are out of scope."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET in the environment, or an eumdac credentials file at ~/.eumdac/credentials (free with sign-up — register at https://user.eumetsat.int, then create the key at https://api.eumetsat.int/api-key/).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - ir
    - vis
    - ir_105
    - vis_06
    - wv_63
    - IR_108
    - VIS006
    - WV_062
  openclaw:
    requires:
      env:
        - EUMETSAT_CONSUMER_KEY
        - EUMETSAT_CONSUMER_SECRET
    primaryEnv: EUMETSAT_CONSUMER_KEY
---

# meteosat-fetch

Downloads Meteosat Level 1 scans, calibrates them with satpy, keeps only the `--bbox`, and writes one variable per band on a regular lat/lon grid.

A scan is one full image of the Earth disk, taken every 10 minutes (FCI) or 15 minutes (SEVIRI). The skill is built for a handful of chosen times (before and after a storm, a few views through one afternoon), not for hourly or daily series; see [Request size](#request-size).

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --time HH:MM [--time HH:MM ...] --bbox N/W/S/E -v {vis,ir,BAND}[,...] [--service 0deg|iodc] [--instrument fci|seviri] [--max-scans N] [--workers N] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--service 0deg|iodc] [--instrument fci|seviri]
```

### Arguments
- `--date` — UTC day of the scans, `YYYY-MM-DD`. One day per call; run once per day for times spanning midnight.
- `--time` — UTC `HH:MM`; repeat or comma-separate for several. Each is floored to its repeat-cycle start (10 min FCI, 15 min SEVIRI); times in the same cycle are fetched once.
- `--bbox` — `N/W/S/E` decimal degrees. Required; whole disks are never stored.
- `--variable`, `-v` — bands; repeat or comma-separate. `vis` is every solar channel, `ir` every thermal and water-vapour channel, or name bands (case-insensitive). FCI: `vis_04 vis_05 vis_06 vis_08 vis_09 nir_13 nir_16 nir_22`, `ir_38 wv_63 wv_73 ir_87 ir_97 ir_105 ir_123 ir_133`. SEVIRI: `VIS006 VIS008 IR_016`, `IR_039 WV_062 WV_073 IR_087 IR_097 IR_108 IR_120 IR_134`.
- `--service` — `0deg` (prime satellite at 0°) or `iodc` (Indian Ocean, 45.5°E, SEVIRI only; a better view of East Africa and the Horn). Default: `0deg`.
- `--instrument` — `fci` or `seviri`. Default: `fci` for `0deg` from 2024-09-24, else `seviri`.
- `--max-scans` — refuse requests above this many scans (after flooring `--time` to cycles). Default: 12. Raise it only after checking the download size below.
- `--workers` — concurrent file downloads per scan. Default: 4. Only matters for FCI, whose scan is several files.
- `--probe-latest` — print the day of the latest scan (`YYYY-MM-DD`) for `--service`/`--instrument` and exit; stderr gives its `HH:MM` UTC. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, one variable per band named as above. `vis` bands are reflectance in `%` (`toa_bidirectional_reflectance`), `ir` bands brightness temperature in `K` (`toa_brightness_temperature`).
- Regular grid at the finest requested band's native sampling: 0.01° FCI visible, 0.02° FCI infrared only, 0.03° SEVIRI. Nearest-neighbour resampled; pixels off the disk are NaN.
- `time` is the repeat-cycle start; coord `scan_start` is the sensing start. The scan sweeps the disk over 10–12 minutes, so a region is imaged a few minutes after either.
- Attrs `platform`, `instrument`, `eumetsat_service`, `sub_satellite_longitude`; `weather_skills_source=eumetsat-datastore:<collection id>`.

### Notes
- Set `EUMETSAT_CONSUMER_KEY` and `EUMETSAT_CONSUMER_SECRET` in the environment for every call, including `--probe-latest`; without them (or `~/.eumdac/credentials`) it exits 2. Never put the values on the command line.
- Visible bands (`vis`, including FCI `nir_*` and SEVIRI `IR_016`) are dark at night; use infrared bands for night-time views. A scan missing from the archive exits 1; try a neighbouring time.
- `--bbox` must lie on the disk. Near the disk edge (high latitudes, or far east or west of the satellite) pixels are stretched and the far side is NaN; stderr warns when a band is more than half missing.

### Request size
EUMETSAT serves only whole-disk files, so the download does not shrink with a small box the way the output does.

| Instrument | Download per scan | What sets it |
|---|---|---|
| SEVIRI (`0deg`, `iodc`) | ~180 MB | Always the full disk, whatever the box or bands. |
| FCI (`0deg`) | ~200–300 MB for a Kenya-sized box | The disk comes in 40 west–east strips; only the strips covering the box's latitude range (plus one either side) are downloaded. A taller box downloads more; a wider one does not. |

The number of bands does not change the download (every file holds all channels), only the output size. So the cost is roughly scans × the figure above: 12 FCI scans ≈ 3 GB, a full day (144 FCI scans) ≈ 35 GB. Stderr prints the estimate before downloading and warns above 4 scans or 1.5 GB. Above `--max-scans` the call exits 2 without downloading.

For a long rainfall or cloud record, use a gridded precipitation product (IMERG, GSMaP, CHIRPS) instead.

### Choosing bands
Band names give the wavelength in µm (`ir_105` = 10.5 µm, `IR_108` = 10.8 µm).

| To see | FCI | SEVIRI | Read it as |
|---|---|---|---|
| Clouds and fog by day | `vis_06` | `VIS006` | Bright = thick cloud or fog; dark = clear land or sea. |
| Cloud-top height, day or night | `ir_105` | `IR_108` | Colder = higher tops; below ~220 K is deep convection. |
| Mid- and upper-level moisture | `wv_63` | `WV_062` | Cold = moist upper air; warm = dry. |
| Dust, fog-vs-low-cloud, ash | `ir_87`, `ir_105`, `ir_123` | `IR_087`, `IR_108`, `IR_120` | Differences between these bands (as in the EUMETSAT dust RGB). |

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-06 --time 12:00 --time 13:00 \
  --bbox 5.5/33.9/-4.7/41.9 -v vis_06,ir_105 -o fci_kenya.zarr
```
Writes two FCI scans over Kenya, 12:00 and 13:00 UTC, with 0.6 µm reflectance and 10.5 µm brightness temperature on a 0.01° grid (about 0.5 GB downloaded).

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-06 --time 21:00 \
  --bbox 12.5/38/-2/51.5 --service iodc -v IR_108,WV_062 -o seviri_horn_night.zarr
```
Writes one night-time SEVIRI scan of the Horn of Africa from the Indian Ocean satellite, which views the region less obliquely than the 0° one.

## Details

| `--service` | `--instrument` | Collection | Satellite | Repeat |
|---|---|---|---|---|
| `0deg` | `fci` | `EO:EUM:DAT:0662` | MTG-I1 (Meteosat-12) | 10 min |
| `0deg` | `seviri` | `EO:EUM:DAT:MSG:HRSEVIRI` | MSG (Meteosat-11/10) | 15 min |
| `iodc` | `seviri` | `EO:EUM:DAT:MSG:HRSEVIRI-IODC` | MSG (Meteosat-9) | 15 min |

There is no FCI Indian Ocean service, and SEVIRI HRV is not offered. FCI L1c starts 2024-09-24. Raw files go to a temporary directory and are deleted after each scan.

---
name: kmsa-wrf-fetch
description: Fetch a KMSA (Kenya Meteorological Department) WRF deterministic forecast — one `YYYYMMDD_to_YYYYMMDD_fcst.nc` per init, ~0.036° (~4 km) over Kenya, 3-hourly out to 192 h from a 06 UTC start — and write a weather-skills standard dataset Zarr. Default source is the public bucket `sheerwater-public-datalake/kmsa-wrf` (no credentials); `--source ssh` reads the KMSA host over SFTP and needs secrets KMSA_WRF_SSH_HOST plus KMSA_WRF_SSH_PASSWORD or KMSA_WRF_SSH_KEY. Default `-v tp` (rainc + rainnc). Fetch writes `tp` as a per-step rate (`mm day-1`) — do not run deaccumulate after this skill.
license: MIT
compatibility: Requires Python 3.12 and uv. --source gcs reads gs://sheerwater-public-datalake/kmsa-wrf anonymously over HTTPS. --source ssh needs SSH/SFTP access to the KMSA WRF host (KMSA_WRF_SSH_HOST, KMSA_WRF_SSH_USER, KMSA_WRF_SSH_PASSWORD or KMSA_WRF_SSH_KEY, KMSA_WRF_REMOTE_DIR).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
    - rainc
    - rainnc
    - t2m
  openclaw:
    requires:
      env: []
    envVars:
      - name: KMSA_WRF_SSH_HOST
        description: KMSA WRF host for --source ssh (host, user@host, or user@host:port)
      - name: KMSA_WRF_SSH_USER
        description: SSH user (optional when given in KMSA_WRF_SSH_HOST)
      - name: KMSA_WRF_SSH_PASSWORD
        description: SSH password (or set KMSA_WRF_SSH_KEY)
      - name: KMSA_WRF_SSH_KEY
        description: Path to an SSH private key file (alternative to the password)
      - name: KMSA_WRF_SSH_PORT
        description: SSH port (optional, default 22)
      - name: KMSA_WRF_SSH_HOST_KEY
        description: Optional pinned server key, e.g. "ssh-ed25519 AAAA..."
      - name: KMSA_WRF_REMOTE_DIR
        description: Remote folder holding the *_fcst.nc files (or pass --remote-dir)
---

# kmsa-wrf-fetch

Finds the KMSA WRF forecast file for an init date (or the latest), downloads
it, maps it onto the weather-skills standard dataset, and writes a local Zarr.

Each init is one netCDF named `YYYYMMDD_to_YYYYMMDD_fcst.nc`; the **first**
date is the init day. The second date is init + 7 days, but the file runs a
day further: 65 three-hourly times from 06 UTC on the init day to +192 h. A
sibling `*_fcst.txt` is ignored.

## When to use

- A task needs the KMSA high-resolution WRF forecast for Kenya (rain or 2 m
  temperature) for clipping, aggregation, comparison, or plotting.
- Not for the KMSA ECMWF S2S products or their CHIRPS-resolution downscales —
  use `kenya-forecast-fetch` for those.

## Sources

| `--source` | Where | Credentials |
|---|---|---|
| `gcs` (default) | `--gcs-path` (default `sheerwater-public-datalake/kmsa-wrf`), read over HTTPS | none |
| `ssh` | `--remote-dir` (default `$KMSA_WRF_REMOTE_DIR`) on the KMSA host, over SFTP | see below |

`--input-file PATH` converts a local file of the same layout instead.

### SSH secrets

On the first `--source ssh` call (including `--probe-latest`), inject these as
environment variables — do not run once without them and retry:

- `KMSA_WRF_SSH_HOST` — `host`, `user@host`, or `user@host:port`.
- `KMSA_WRF_SSH_PASSWORD`, or `KMSA_WRF_SSH_KEY` (path to a private key).
- `KMSA_WRF_SSH_USER` / `KMSA_WRF_SSH_PORT` — only when not in the host string.
- `KMSA_WRF_REMOTE_DIR` — the folder, unless `--remote-dir` is passed.
- `KMSA_WRF_SSH_HOST_KEY` — optional; pins the server key
  (`ssh-ed25519 AAAA...`). Without it a key in `~/.ssh/known_hosts` is
  checked and an unknown key is accepted with a warning.

Never print the values.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--source gcs|ssh] [--date YYYY-MM-DD] \
    [--bbox N/W/S/E] [-v VAR ...] [--gcs-path BUCKET/PREFIX] [--remote-dir DIR] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --input-file <YYYYMMDD_to_YYYYMMDD_fcst.nc> -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--source gcs|ssh] --probe-latest
```

### Arguments

- `--date` — init date `YYYY-MM-DD` (matched against the file name's first
  date). Default: the latest file. Latest published init: `--probe-latest`.
- `--bbox` — optional `N/W/S/E` subset of the ~0.036° Kenya domain
  (about 5.55°S–6.19°N, 32.67–43.11°E).
- `--variable`, `-v` — repeatable. `tp` (default; `rainc + rainnc`), `rainc`
  (cumulus), `rainnc` (grid-scale), `t2m` (2 m temperature; alias `t2`).
- `--source`, `--gcs-path`, `--remote-dir`, `--input-file` — see **Sources**.
- `--probe-latest` — print the latest init `YYYY-MM-DD` (or `none`) and exit.
- `--output`, `-o` — output Zarr path.

## Output

Classic deterministic forecast: scalar `time` (the 06 UTC init), `step`,
`latitude`, `longitude` (the `lev` level is dropped).

- `rainc` / `rainnc` in the file are accumulated since init (mm). Fetch
  deaccumulates them into per-step **rates** (`mm day-1`), so `step` has 64
  intervals labeled at each interval's left edge (`0 h` = 06–09 UTC on the
  init day, last `189 h`). Do **not** run `deaccumulate`.
- `t2m` is converted from K to `degree_Celsius` and shares that 64-step axis:
  each value is the instantaneous temperature at the interval's start; the
  final +192 h instant is dropped.
- `data_interval` is `3 hour`. For daily or weekly totals run
  `aggregate-temporal --period daily|weekly`, then `convert-to-totals`.
  Days run 06–06 UTC from the init, not 00–00 UTC.

The grid is ~0.036° with slightly uneven latitude spacing (the model's
Mercator rows). To compare against CHIRPS / IMERG cell by cell, use
`coarsen --reference-grid` onto the obs grid first; plotting needs no regrid.

Stamped `weather_skills_source=kmsa-wrf:<origin>` (the bucket URL, `ssh:` file
name, or local file name) and the usual `weather_skills_history`.

## Examples

```bash
# Latest init from the public bucket, precip only
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py -o /tmp/wrf_tp.zarr

# One init, rain + temperature, over Nairobi
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-04 -v tp -v t2m \
    --bbox -0.9/36.6/-1.5/37.2 -o /tmp/wrf_nbo.zarr

# From the KMSA host over SSH (secrets injected as env vars)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --source ssh --date 2026-10-04 -o /tmp/wrf_ssh.zarr

# Daily totals for plotting
uv run ${CLAUDE_SKILL_DIR}/../aggregate-temporal/scripts/aggregate.py \
    -i /tmp/wrf_tp.zarr -o /tmp/wrf_daily.zarr --period daily
uv run ${CLAUDE_SKILL_DIR}/../convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/wrf_daily.zarr -o /tmp/wrf_daily_mm.zarr
```

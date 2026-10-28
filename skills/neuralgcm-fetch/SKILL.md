---
name: neuralgcm-fetch
description: "Fetch a NeuralGCM subseasonal ensemble forecast (up to 50 members, 6-hourly leads to 60 days, ~2.8° global) for one init or a range of inits over an optional lat/lon box, as a weather-skills Zarr: IMERG-trained precipitation, or 2 m temperature and dewpoint. Reads the private Tomorrow Now 2026 realtime archive in gs://neuralgcm-s2s with Google Cloud service-account credentials. Use when a task needs the NeuralGCM forecast."
license: MIT
compatibility: "Requires Python 3.12 and uv. Reads private consolidated Zarr from gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1 via gcsfs. Requires Google Cloud credentials: GCLOUD_SERVICE_CREDENTIALS (the raw service-account key JSON, for environments that can only inject secret values), GOOGLE_APPLICATION_CREDENTIALS (a service-account JSON file path), or Application Default Credentials from `gcloud auth application-default login`."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.6"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - d2m
  openclaw:
    requires:
      env:
        - GCLOUD_SERVICE_CREDENTIALS
    primaryEnv: GCLOUD_SERVICE_CREDENTIALS
    envVars:
      - name: GCLOUD_SERVICE_CREDENTIALS
        description: Full contents of a GCP service-account key JSON with read access to gs://neuralgcm-s2s. Use this when the runtime can only inject secret values, not files.
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON file with read access to gs://neuralgcm-s2s. Used only if GCLOUD_SERVICE_CREDENTIALS is unset.
---

# neuralgcm-fetch

Fetches one NeuralGCM S2S ensemble init, or every init in one or more date windows, from `gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1/` as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--dataset imerg:precip|era5:surface] [--date YYYY-MM-DD] [--cycle 00|12] [--bbox N/W/S/E] [-v VAR ...] -o OUT.zarr
# Every 00 UTC init in a window, in one call; repeat the pair for several windows
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--start-time ... --end-time ...] [--cycle 00|12] [--bbox N/W/S/E] [-v VAR ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET] [--cycle HH]
```

The bucket is private. Set one of, in order of precedence: `GCLOUD_SERVICE_CREDENTIALS` (the service-account key JSON itself), `GOOGLE_APPLICATION_CREDENTIALS` (path to that JSON file), or Application Default Credentials from `gcloud auth application-default login`.

### Arguments
- `--dataset` — product under each init. Default: `imerg:precip` (alias `precip`) → `tp`. `era5:surface` (alias `surface`) → `t2m`, `d2m`.
- `--date` — init date. Default: the latest init that has `--dataset`. A missing init exits 1 and lists the nearest published inits; nothing is substituted. Pick one of those rather than trying other dates.
- `--start-time`, `--end-time` — an init window, both days inclusive. Repeat the pair for several windows, one window per pair. Takes the `--cycle` init (default 00 UTC) of every published day in the windows with all its leads: like `--date`, the windows select init times, not valid times. Use this instead of one `--date` call per init; not with `--date`. An init without a `--dataset` store is skipped with a `WARNING` on stderr; no init in any window exits 1.
- `--cycle` — init hour UTC: `00` or `12`. Default: `00` with `--date` or windows, otherwise the newest cycle.
- `--bbox` — `N/W/S/E` decimal degrees; negative longitudes work. Default: the full global grid.
- `--variable`, `-v` — one field per flag; repeat for several. Default: every field in the product. Aliases: `tp`/`precip`/`pr`/`total_precipitation`, `t2m`/`2m_temperature`, `d2m`/`2m_dewpoint_temperature`. A name not in the product exits and lists `Available:`.
- `--probe-latest [DATASET]` — print the latest init date (or `none`) for that dataset id (default `imerg:precip`) and exit; honours `--cycle`.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(number, step, latitude, longitude)` with scalar `time` = init (00 or 12 UTC); with windows, `(time, step, number, latitude, longitude)`, one `time` per init; `number` spans the largest init, and an init with fewer members is NaN in the rest (not filled). `number` is 0..N−1; N varies by init (up to 50) and is printed to stderr.
- `tp` is a rate in `mm day-1`, clipped at zero, labelled at the start of its 6 h period (`step=0` is `[init, init+6h)`).
- `t2m` and `d2m` are `degree_Celsius`, instants at `init + step`; the first step is 6 h.
- Stamped `weather_skills_source=neuralgcm:<dataset>:<init stamp>` (`<first>..<last>` with windows).
- With windows, an init still publishing its long leads keeps them NaN (not filled), with a `WARNING` naming the init.

### Notes
- Some 00/12 UTC inits are missing from the archive; use `--probe-latest` or omit `--date`.
- Window size: each init is loaded whole for the box, then the inits are joined, so memory is about inits × members × steps × grid cells × 4 bytes per field, doubled while joining. A global init is about 0.4 GB per field; a country-size `--bbox` is under 1 MB. Keep global windows to about 10 inits; a boxed window can cover a year.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v tp -o neuralgcm_kenya.zarr
```
Writes the 00 UTC ensemble's 6-hourly precip rates over Kenya for 60 days.
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-03-01 --end-time 2026-05-31 --bbox 5.5/33.5/-5/42.5 -v tp -o neuralgcm_kenya_mam.zarr
```
Writes every published 00 UTC init from March to May over Kenya, one `time` per init.

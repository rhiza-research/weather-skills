---
name: neuralgcm-fetch
description: "Fetch a NeuralGCM subseasonal ensemble forecast (up to 50 members, 6-hourly leads to 60 days, ~2.8° global) for one init over an optional lat/lon box, as a weather-skills Zarr: IMERG-trained precipitation, or 2 m temperature and dewpoint. Reads the private Tomorrow Now 2026 realtime archive in gs://neuralgcm-s2s with Google Cloud service-account credentials. Use when a task needs the NeuralGCM forecast."
license: MIT
compatibility: "Requires Python 3.12 and uv. Reads private consolidated Zarr from gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1 via gcsfs. Requires Google Cloud credentials: GCLOUD_SERVICE_CREDENTIALS (the raw service-account key JSON, for environments that can only inject secret values), GOOGLE_APPLICATION_CREDENTIALS (a service-account JSON file path), or Application Default Credentials from `gcloud auth application-default login`."
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.4"
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

Fetches one NeuralGCM S2S ensemble init from `gs://neuralgcm-s2s/staging/realtime/tomorrow_now_2026/v1/` as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--dataset imerg:precip|era5:surface] [--date YYYY-MM-DD] [--cycle 00|12] [--bbox N/W/S/E] [-v VAR ...] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [DATASET] [--cycle HH]
```

The bucket is private. Set one of, in order of precedence: `GCLOUD_SERVICE_CREDENTIALS` (the service-account key JSON itself), `GOOGLE_APPLICATION_CREDENTIALS` (path to that JSON file), or Application Default Credentials from `gcloud auth application-default login`.

### Arguments
- `--dataset` — product under each init. Default: `imerg:precip` (alias `precip`) → `tp`. `era5:surface` (alias `surface`) → `t2m`, `d2m`.
- `--date` — init date. Default: the latest init that has `--dataset`. A missing init exits 1; nothing is substituted.
- `--cycle` — init hour UTC: `00` or `12`. Default: `00` with `--date`, otherwise the newest cycle.
- `--bbox` — `N/W/S/E` decimal degrees; negative longitudes work. Default: the full global grid.
- `--variable`, `-v` — one field per flag; repeat for several. Default: every field in the product. Aliases: `tp`/`precip`/`pr`/`total_precipitation`, `t2m`/`2m_temperature`, `d2m`/`2m_dewpoint_temperature`. A name not in the product exits and lists `Available:`.
- `--probe-latest [DATASET]` — print the latest init date (or `none`) for that dataset id (default `imerg:precip`) and exit; honours `--cycle`.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(number, step, latitude, longitude)` with scalar `time` = init (00 or 12 UTC). `number` is 0..N−1; N varies by init (up to 50) and is printed to stderr.
- `tp` is a rate in `mm day-1`, clipped at zero, labelled at the start of its 6 h period (`step=0` is `[init, init+6h)`).
- `t2m` and `d2m` are `degree_Celsius`, instants at `init + step`; the first step is 6 h.
- Stamped `weather_skills_source=neuralgcm:<dataset>:<init stamp>`.

### Notes
- Some 00/12 UTC inits are missing from the archive; use `--probe-latest` or omit `--date`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-12 --bbox 5.5/33.5/-5/42.5 -v tp -o neuralgcm_kenya.zarr
```
Writes the 00 UTC ensemble's 6-hourly precip rates over Kenya for 60 days.

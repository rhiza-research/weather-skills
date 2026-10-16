---
name: clim-fetch
description: Fetch a precomputed daily climatology (avg + std) for a `--dataset` (imerg_final, era5, chirps, ecmwf_ifs, oisst, ...) from Sheerwater's public GCS mirror, select one `--prediction-timedelta` lead, optionally roll it up to a coarser `--window` in days, and expand it onto a requested `--start-time`/`--end-time` calendar window, so timestamps line up with the rest of a pipeline's data. Precipitation sources default to `--variable precip`; OISST sea-surface temperature uses `--dataset oisst --variable sst`. Optional `--bbox N/W/S/E` (compose with resolve-region) subsets before download. Output vars are `<var>_avg` / `<var>_std` (e.g. `precip_avg`), so rename `precip_avg` to `precip` before `difference` against obs. No ERA5 temperature climatology (era5 is precip only); an unknown pair exits listing the available ones. Use when a task needs a climatological baseline for anomalies, verification, or comparison — not live observations (use dynamical-fetch, chirps-fetch, oisst-fetch, etc.).
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a static climatology Zarr from the public GCS bucket sheerwater-public-datalake over anonymous HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - sst
---

# clim-fetch

Reads a static day-of-year climatology Zarr mirrored to a public GCS bucket
(`gs://sheerwater-public-datalake/climatologies/<dataset>_<variable>_<window>d.zarr`
— daily is `window=1`, one naming convention for every window). The source
Zarr has dims `init_time`, `prediction_timedelta`, `lat`, `lon` —
1904 init dates (a leap year, so every day-of-year 1..366 has a row). The
multi-lead mirrors (`ecmwf_ifs`, `gefs`) hold no value for an init whose
valid date runs past Dec 31, so at `--prediction-timedelta N` the dates
Jan 1..Jan N come out NaN (never filled; stderr lists every date with no
value anywhere in the grid). This skill:

1. Selects one lead via `--prediction-timedelta` (days, default `0`) and
   realizes `time = init_time + prediction_timedelta`.
2. Optionally rolls the climatology up to a coarser `--window` (days) —
   see "Aggregating to a coarser window" below.
3. Gives each date in `[--start-time, --end-time]` the climatology row for
   the same month and day in the 1904 cycle (not the same day-of-year: in a
   non-leap year Mar 1 is day 60, which in 1904 is Feb 29), repeating rows
   for windows spanning more than one year. Feb 29 is used only in leap
   years.

One skill covers every mirrored climatology — the source is selected with
`--dataset`.

## When to use

- Need a climatological baseline (mean + spread) to build an anomaly map or
  feed `difference` against live observations or a forecast.
- Want climatology output with real calendar timestamps that match another
  dataset's `time` dim, instead of a bare day-of-year axis.
- Need the climatology at a specific forecast lead (e.g. a 7-day-ahead
  climatological baseline) rather than just the init-date value.

Not for live observations — use `imerg-fetch` / `dynamical-fetch` for IMERG,
`chirps-fetch` for live CHIRPS, `arco-era5-fetch` for ERA5, `oisst-fetch` for
live OISST SST, etc. Not for datasets not yet mirrored — see "Supported
datasets" below.

### Supported datasets

`--dataset` must be the exact bucket product prefix — no aliasing.
`--variable` is part of the object key
(`climatologies/<dataset>_<variable>_<window>d.zarr`); the default `precip`
is correct for the precipitation sources below. OISST SST is a different
variable — pass `--variable sst`.

| `--dataset` | Source | Grid | Leads (days) |
| --- | --- | --- | --- |
| `imerg_final` | IMERG final daily precipitation climatology (`--variable precip`) | 0.1° | 0 |
| `era5` | ERA5 daily climatology (`--variable precip`) | 0.25° | 0 |
| `chirps` | CHIRPS daily precipitation climatology (`--variable precip`); same cell centers as `chirps-fetch` | 0.05° | 0 |
| `ecmwf_ifs` | ECMWF IFS reforecast daily climatology — `precip`, `sst`, `uwind10m`, `vwind10m` | 1.5° | 0–45 |
| `oisst` | NOAA OISST v2.1 daily sea-surface temperature climatology. Requires `--variable sst`. For live SST observations use `oisst-fetch`, not this skill. | 0.25° | 0 |
| `gefs` | GEFS reforecast daily climatology — `precip`, `tmp2m` (2m temperature) | 1° | 0–34 |

More datasets are added by mirroring a new Zarr under the same bucket
convention — no CLI change needed once added. A `--dataset`/`--variable`
pair that is not mirrored (e.g. `--dataset era5 --variable tmp2m`) exits
non-zero and lists every available pair. There is no ERA5 or observed
temperature climatology; the only 2 m temperature one is the model
reforecast `--dataset gefs --variable tmp2m`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset <id> --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o <path.zarr> \
  [--variable precip|sst] [--prediction-timedelta 0] [--window 1] [--align left] [--bbox N/W/S/E]
```

### Arguments

- `--dataset` — climatology source id (see table above).
- `--start-time`, `--end-time` — inclusive calendar window, absolute ISO dates
  `YYYY-MM-DD`. The output has one row per calendar day in this window.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--variable`, `-v` — climate variable (default: `precip`). Selects the
  remote object key `climatologies/<dataset>_<variable>_<window>d.zarr`,
  and is also a fallback name if the cached Zarr has no `variable` global
  attr of its own. Precipitation sources use the default. For OISST SST
  pass `--dataset oisst --variable sst` (the default `precip` looks for a
  store that does not exist).
- `--prediction-timedelta` — forecast lead in whole days to select from the
  source's `prediction_timedelta` dim (default: `0`). Errors listing the
  available leads if the requested value isn't cached.
- `--window` — climatology window in whole days (default: `1`, i.e. daily).
  See "Aggregating to a coarser window" below.
- `--align` — `left` (default), `right`, or `center`; how a locally-rolled
  `--window` labels each window (matches `aggregate-temporal`'s own
  `--align` default of `left`). Ignored (no-op) on `--window 1` or when a
  pre-aggregated cache is hit.
- `--bbox` — optional `N/W/S/E` bounding box; use `resolve-region` to turn a
  country or named region into this value first. Applied before the
  day-of-year expansion, on the still-lazy remote Zarr, so only the chunks
  overlapping the bbox are pulled from GCS. Each chunk is 300×300 cells
  × a whole year, so even a small bbox reads whole chunks (a Kenya CHIRPS
  fetch peaks at ~1.4 GB RSS). Always pass `--bbox` for `chirps` and
  `imerg_final`: without it the whole global year is read (tens of GB).

### Aggregating to a coarser window

Averaging a `_std` field the same way you average `_avg` computes the wrong
statistic. `--window` handles this correctly so the agent never has to run a
separate aggregation step itself which is prone to errors:

1. Tries a pre-aggregated cache first:
   `climatologies/<dataset>_<variable>_<window>d.zarr`.
2. If that isn't available, falls back to the daily climatology and rolls it
   up locally with a `--window`-day window, labeled per `--align` (e.g.
   `--window 7 --align left` on day-of-year 100 averages days 100–106):
   - `<variable>_avg` rolls like an ordinary rate.
   - `<variable>_std` is not the mean of the daily stds. It assumes
     independent days: `Std(window mean) = sqrt(mean(std_i²)) / sqrt(window)`.
     With the positive day-to-day correlation real rainfall has, this is a
     lower bound — the true std lies between it and `mean(std_i)`. A
     warning prints to stderr whenever this local roll-up is used (no
     pre-aggregated `--window` cache is currently mirrored, so that is
     every `--window` > 1).
3. The climatology is circularly padded by `window` days on each end
   before rolling (day 366 wraps to day 1), so days near Jan 1 / Dec 31 get a
   full window instead of coming out `NaN` at the edges.

### Output

Zarr with dims `latitude`, `longitude`, `time` (in that order) where `time` is exactly the requested
calendar window (real dates, not the source's 1904 placeholder). Data
variables are named `<variable>_avg` (climatological mean) and
`<variable>_std` (climatological standard deviation), where `<variable>`
comes from the cached Zarr's own `variable` global attr (e.g. `precip_avg`,
`precip_std`, or `sst_avg` / `sst_std` for `--dataset oisst`). Recognized
kinds (currently `precip`, air `temp`) are converted to standard display
units (e.g. `mm day-1` for precip); anything else (e.g. `sst`, `uwind10m`,
`vwind10m`, `tmp2m`) passes through unconverted, with units from the source
(per-variable, else dataset-level, else a small hardcoded table in
`fetch.py` — `_KNOWN_UNITS` — for mirrors with no usable units metadata,
whether missing entirely or present but not a parseable unit string, e.g.
gefs tmp2m's raw `"avg. daily C"`; currently `degree_Celsius` for `sst` and
`tmp2m`, `m/s` for `uwind10m`/`vwind10m`). The precip std is converted with its mean (a pure scale,
so that is safe). No mirrored temperature source needs a unit conversion;
a std must never take a temperature offset (`standardize-anomaly` converts
a std by scale only).
Global attrs include `weather_skills_source=sheerwater-mirror:<dataset>`,
`climatology_dataset`, `climatology_variable`,
`climatology_prediction_timedelta_days`, `climatology_window_days`.
Stamped with `data_interval` `1 day` (output is always one row per calendar
day, regardless of `--window`) and, per variable, `aggregation_period`
`"<window> day"` (the width of window each value represents — the two attrs
mean different things: see `aggregate-temporal`'s docs on the same
distinction for its own `--window`).

Two dates far apart within the window but sharing a day-of-year (e.g. two
different years' June 15) get **identical** climatology values by design —
that is the point of the day-of-year gather.

### Anomalies against observations: match the variable name

`difference` subtracts variables **by name**, and only names present in
both inputs. An observation fetcher writes `precip` (or `sst`) while this
skill writes `precip_avg` / `precip_std`, so the pair shares no variable.
Rename the climatology mean first, then difference:

```bash
uv run skills/edit-coords/scripts/edit_coords.py -i clim.zarr -o clim_precip.zarr \
    --rename precip_avg=precip
uv run skills/difference/scripts/difference.py -i obs.zarr -i clim_precip.zarr \
    -v precip -o anom.zarr
```

Both inputs must be on the same footing first (both rates, or both totals
over the same window — see the weekly recipe below). For a standardized
anomaly, `standardize-anomaly --climatology` reads `<var>_avg` / `<var>_std`
directly; no rename needed.
### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

```bash
# IMERG climatology for calendar year 2020, at init (lead 0).
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  -o /tmp/imerg_clim_2020.zarr

# Same, but the 7-day-ahead lead's climatology.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  --prediction-timedelta 7 -o /tmp/imerg_clim_2020_7d.zarr

# A 7-day (weekly) smoothed climatology instead of daily.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  --window 7 -o /tmp/imerg_clim_2020_weekly.zarr

# ERA5 climatology spanning two years — June 2020 through June 2021 repeats
# each day-of-year's row across the two Junes.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset era5 --start-time 2020-06-01 --end-time 2021-06-30 \
  -o /tmp/era5_clim.zarr

# Kenya only — resolve-region first, then pass its bbox through.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  --bbox 5.5/33.9/-4.7/41.9 -o /tmp/imerg_clim_2020_kenya.zarr

# ECMWF IFS sst / wind components — this mirror has no units metadata at
# all for these; the skill falls back to its own hardcoded known units
# (degree_Celsius for sst, m/s for wind components), no flag needed.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset ecmwf_ifs --variable sst \
  --start-time 2020-01-01 --end-time 2020-12-31 -o /tmp/ecmwf_sst_2020.zarr

uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset ecmwf_ifs --variable uwind10m \
  --start-time 2020-01-01 --end-time 2020-12-31 -o /tmp/ecmwf_uwind10m_2020.zarr

uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset ecmwf_ifs --variable vwind10m \
  --start-time 2020-01-01 --end-time 2020-12-31 -o /tmp/ecmwf_vwind10m_2020.zarr

# OISST sea-surface temperature climatology. --variable sst is required;
# the default precip key is not mirrored for this dataset. For live SST
# observations use oisst-fetch, then difference / standardize-anomaly
# against this climatology.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset oisst --variable sst --start-time 2020-01-01 --end-time 2020-12-31 \
  -o /tmp/oisst_clim_2020.zarr
```

### Recipe: weekly accumulation climatology (3-skill)

`--window` rolls up climatological *rates* (mean and std of the daily rate
averaged over the window) — it does not produce a climatology of *totals*.
For a weekly-accumulation climatology (e.g. "typical weekly CHIRPS rainfall
total"), compose with `select` and `convert-to-totals`: `clim-fetch --window
7` still emits one row per calendar day (overlapping 7-day windows), so
`select` first thins to one row per week before `convert-to-totals` turns
each row's mean/std rate into a total — this is correct for both `_avg` and
`_std` since totaling is linear (`Std(sum) = window × Std(mean)`, the same
relationship `convert-to-totals`'s `rate_to_total` already applies uniformly
to every precipitation variable, mean or std alike):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset chirps --start-time 2020-01-01 --end-time 2020-12-31 \
  --window 7 -o /tmp/chirps_clim_weekly_rate.zarr

# Thin to non-overlapping weeks (every 7th day) before totaling.
uv run ${CLAUDE_SKILL_DIR}/../select/scripts/select_dim.py \
  -i /tmp/chirps_clim_weekly_rate.zarr -o /tmp/chirps_clim_weekly_rate_thinned.zarr \
  --dim time --value 2020-01-01 --value 2020-01-08 --value 2020-01-15 # ... etc.

uv run ${CLAUDE_SKILL_DIR}/../convert-to-totals/scripts/convert_to_totals.py \
  -i /tmp/chirps_clim_weekly_rate_thinned.zarr -o /tmp/chirps_clim_weekly_total.zarr
```

The result carries `_avg`/`_std` totals in mm, ready to feed
`standardize-anomaly --climatology` against an observed weekly-accumulated
field (see that skill's own docs for the recipe from the other side).

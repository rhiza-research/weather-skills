---
name: verify
description: "Score a forecast against observations on a shared grid or shared stations, cell by cell: event hits, misses and false alarms at a threshold (hit rate, FAR, CSI, balanced error rate, frequency bias), bias (forecast − obs), or mean absolute error, for every forecast lead in one call. Writes the metric field as a Zarr and prints a regional score. Use for forecast skill maps and event verification."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.10"
  catalog-group: transforms
---

# verify

Compares a forecast with observations cell by cell (or station by station) and writes one metric field.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast FC.zarr --obs OBS.zarr [--metric hits|bias|mae] [-v NAME] [--threshold 1] [--member-fraction 0.5] -o OUT.zarr
```

### Arguments
- `--forecast` — forecast Zarr.
- `--obs` — observation Zarr on the forecast's grid spacing (or the same stations).
- `--metric` — `hits` (event classification), `bias` (forecast − obs), or `mae` (|forecast − obs|). Default: `hits`.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable (names may differ).
- `--threshold` — event cutoff for `hits`, a plain number in the forecast's units: a cell counts as an event when its value is at least this (`10` on `mm day-1` data means 10 mm per day). Convert other cutoffs yourself first: 50 mm in 3 days on 3-day mean rates is `16.67`. Default: `1`. Ignored (with a stderr note) for other metrics.
- `--member-fraction` — `hits` on an ensemble only: the forecast event is at least this share of members ≥ threshold (`0.5` = half the members), instead of the ensemble mean ≥ threshold. The mean smooths heavy-rain peaks away; use this for rare events.
- `--output`, `-o` — output Zarr.

### Output
- One variable: `event_hit`, `bias`, or `mae`, float32. `event_hit` is `1` hit (both ≥ threshold), `2` miss (obs only), `3` false alarm (forecast only), `0` correct negative (both below), with CF `flag_values` / `flag_meanings`. Attrs `event_threshold` (in forecast units) and, with `--member-fraction`, `event_member_fraction`.
- `bias` and `mae` are in the forecast's units (a temperature error is `delta_degC`).
- NaN in either input stays NaN and is left out of the score.
- Stdout prints the score: for `hits`, hit rate = hits / observed events, false alarms with FAR = false alarms / forecast events, CSI = hits / (hits + misses + false alarms), and BER (balanced error rate) = the mean of misses / obs events and false alarms / obs non-events, where 0 is perfect and 0.5 is no better than chance whatever the event's base rate, and frequency bias = forecast events / observed events (1 = forecast as often as observed, above 1 = over-forecast); for `bias` / `mae`, a cos-latitude-weighted mean (a plain mean on stations). A forecast with several leads also prints one line per `step`. Also stamped as `verify_score_summary`.

### Notes
- Grid spacing must match: a different lat or lon spacing exits 2. Put the obs on the forecast grid (for fine obs, an area mean such as `coarsen --reference-grid FC.zarr --method conservative`), not the forecast on the obs grid.
- Inputs keep only shared labels (inner join); no overlap on a dim exits 2 and prints both ranges. Single-time inputs must carry the same date.
- **Leads are scored directly.** A forecast with `time` (init, scalar or a range of inits) and `step` against obs on `time` pairs each init and step with the obs at init + step, so no select or step-to-time is needed. The output keeps `(time, step, …)` with `time` = init. Label both on the same bins: after `aggregate-temporal --period "3 day" --time-dim step`, step `3D` covers days 4–6, so the obs need 3-day windows labelled at their first day (`aggregate-temporal --window "3 day" --align left`). Valid times outside the obs record stay NaN and are not scored; none at all exits 2. Obs that also have `step` join on it instead.
- Ensemble `number` is averaged first, so every metric scores the ensemble mean, unless `--member-fraction` counts members instead; a NaN member makes that cell NaN.
- Obs are converted to the forecast's units. Incompatible units (an `mm` total against an `mm day-1` rate), or units on only one input, exit 2.
- Stations: sample the grid at the station points first, then pass the sample and the station Zarr. Pairs join on `station_id` and `time`.

### Examples
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast s2s_kenya_week2_mm.zarr \
  --obs chirps_kenya_week2_mm_on_s2s.zarr --metric hits -v precip --threshold 10 -o hits_week2.zarr
```
Writes week-2 hit / miss / false-alarm / correct-negative cells for ≥ 10 mm weekly rain over Kenya and prints the scores.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast gefs_hindcast_3day.zarr \
  --obs imerg_on_gefs_3day_windows.zarr --metric hits --threshold 16.67 -o hits_3day.zarr
```
Scores every hindcast init and every 3-day lead window for ≥ 50 mm in 3 days (16.67 mm day-1 on 3-day mean rates), with one score line per lead.

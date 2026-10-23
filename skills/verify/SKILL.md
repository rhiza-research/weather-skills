---
name: verify
description: "Score a forecast against observations on a shared grid or shared stations, cell by cell: event hits at a threshold, bias (forecast − obs), or mean absolute error. Writes the metric field as a Zarr and prints a regional score. Use for forecast skill maps and event verification."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# verify

Compares a forecast with observations cell by cell (or station by station) and writes one metric field.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast FC.zarr --obs OBS.zarr [--metric hits|bias|mae] [-v NAME] [--threshold 1] -o OUT.zarr
```

### Arguments
- `--forecast` — forecast Zarr.
- `--obs` — observation Zarr on the forecast's grid spacing (or the same stations).
- `--metric` — `hits` (event classification), `bias` (forecast − obs), or `mae` (|forecast − obs|). Default: `hits`.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable (names may differ).
- `--threshold` — event cutoff for `hits`, in the forecast's units: an event is value ≥ threshold. Default: `1`. Ignored (with a stderr note) for other metrics.
- `--output`, `-o` — output Zarr.

### Output
- One variable: `event_hit`, `bias`, or `mae`, float32. `event_hit` is `1` hit (both ≥ threshold), `-1` disagree (only one is), `0` both below, with CF `flag_values` / `flag_meanings`.
- `bias` and `mae` are in the forecast's units (a temperature error is `delta_degC`).
- NaN in either input stays NaN and is left out of the score.
- Stdout prints the score: hit rate = hits / observed events, or a cos-latitude-weighted mean of bias / MAE (a plain mean on stations). Also stamped as `verify_score_summary`.

### Notes
- Grid spacing must match: a different lat or lon spacing exits 2. Put the obs on the forecast grid (for fine obs, an area mean such as `coarsen --reference-grid FC.zarr --method conservative`), not the forecast on the obs grid.
- Inputs keep only shared labels (inner join); no overlap on a dim exits 2 and prints both ranges. Single-time inputs must carry the same date.
- A forecast `step` dim exits 2 unless the obs have `step`: convert leads to valid times first.
- Ensemble `number` is averaged first, so every metric scores the ensemble mean; a NaN member makes that cell NaN.
- Obs are converted to the forecast's units. Incompatible units (an `mm` total against an `mm day-1` rate), or units on only one input, exit 2. For a precip threshold in mm, give both inputs as period totals.
- Stations: sample the grid at the station points first, then pass the sample and the station Zarr. Pairs join on `station_id` and `time`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast s2s_kenya_week2_mm.zarr \
  --obs chirps_kenya_week2_mm_on_s2s.zarr --metric hits -v precip --threshold 10 -o hits_week2.zarr
```
Writes week-2 hit / disagree / below cells for ≥ 10 mm weekly rain over Kenya and prints the hit rate.

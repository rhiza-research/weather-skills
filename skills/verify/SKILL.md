---
name: verify
description: "Score a forecast against observations on a shared grid or shared stations, cell by cell: event hits at a threshold, bias (forecast − obs), mean absolute error, or the ranked probability skill score (RPSS) of an ensemble against tercile or other category edges. Writes the metric field as a Zarr and prints a regional score. Use for forecast skill maps, event verification, and probabilistic (tercile) skill."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.8"
  catalog-group: transforms
---

# verify

Compares a forecast with observations cell by cell (or station by station) and writes one metric field.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast FC.zarr --obs OBS.zarr [--metric hits|bias|mae|rpss] [-v NAME] [--threshold 1] [--fc-edges FE.zarr --obs-edges OE.zarr] -o OUT.zarr
```

### Arguments
- `--forecast` — forecast Zarr.
- `--obs` — observation Zarr on the forecast's grid spacing (or the same stations).
- `--metric` — `hits` (event classification), `bias` (forecast − obs), `mae` (|forecast − obs|), or `rpss` (ranked probability skill score of the ensemble). Default: `hits`.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable (names may differ).
- `--threshold` — event cutoff for `hits`, in the forecast's units: an event is value ≥ threshold. Default: `1`. Ignored (with a stderr note) for other metrics.
- `--fc-edges`, `--obs-edges` — `rpss` only, both required: category edges with a `quantile` dim, from `summarize-dim --dim time --method quantile -q 0.333 -q 0.667` (add `--dim number` for an ensemble hindcast). Split the forecast by its own model climate and the obs by the observed climate. Any number of quantiles works: K edges give K+1 categories. Converted to the units of the data they split.
- `--output`, `-o` — output Zarr.

### Output
- One variable: `event_hit`, `bias`, `mae`, or `rpss`, float32. `event_hit` is `1` hit (both ≥ threshold), `-1` disagree (only one is), `0` both below, with CF `flag_values` / `flag_meanings`.
- `rpss` has no `time` dim: at each cell it is 1 − ΣRPS / ΣRPS_clim summed over every init, where the reference forecasts each category with probability 1/K. 1 is perfect, 0 is no better than climatology, below 0 is worse. Units `1`; attrs `rpss_categories`, `rpss_quantiles`.
- `bias` and `mae` are in the forecast's units (a temperature error is `delta_degC`).
- NaN in either input stays NaN and is left out of the score.
- Stdout prints the score: hit rate = hits / observed events, a cos-latitude-weighted mean of bias / MAE (a plain mean on stations), or for `rpss` one regional RPSS over every cell and init (1 − Σw·RPS / Σw·RPS_clim). Also stamped as `verify_score_summary`.

### Notes
- Grid spacing must match: a different lat or lon spacing exits 2. Put the obs on the forecast grid (for fine obs, an area mean such as `coarsen --reference-grid FC.zarr --method conservative`), not the forecast on the obs grid.
- Inputs keep only shared labels (inner join); no overlap on a dim exits 2 and prints both ranges. Single-time inputs must carry the same date.
- A forecast `step` dim exits 2 unless the obs have `step`: convert leads to valid times first.
- Ensemble `number` is averaged first, so `hits`, `bias` and `mae` score the ensemble mean; a NaN member makes that cell NaN. `rpss` needs the members (exits 2 without a `number` dim): each category's probability is the share of members in it, a member equal to an edge counts as below it, and a NaN member drops that cell-init.
- Obs are converted to the forecast's units. Incompatible units (an `mm` total against an `mm day-1` rate), or units on only one input, exit 2. For a precip threshold in mm, give both inputs as period totals.
- Stations: sample the grid at the station points first, then pass the sample and the station Zarr. Pairs join on `station_id` and `time`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast s2s_kenya_week2_mm.zarr \
  --obs chirps_kenya_week2_mm_on_s2s.zarr --metric hits -v precip --threshold 10 -o hits_week2.zarr
```
Writes week-2 hit / disagree / below cells for ≥ 10 mm weekly rain over Kenya and prints the hit rate.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast hindcast_week3_on_chirps.zarr \
  --obs chirps_week3.zarr --metric rpss -v precip \
  --fc-edges hindcast_week3_terciles.zarr --obs-edges chirps_week3_terciles.zarr -o rpss_week3.zarr
```
Writes week-3 tercile RPSS per cell over every hindcast init and prints the regional RPSS.

---
name: verify
description: "Score a forecast against observations on a shared grid or shared stations: event hits, bias, MAE, RMSE, ensemble CRPS or Brier score. Writes the metric field as a Zarr and prints a regional score. Use for deterministic and probabilistic forecast verification."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.5"
  catalog-group: transforms
---

# verify

Compares a forecast with observations cell by cell (or station by station) and writes one metric field.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast FC.zarr --obs OBS.zarr [--metric hits|bias|mae|rmse|crps|brier] [-v NAME] [--threshold VALUE] [--reduce DIM ...] [--crps-estimator standard|fair] -o OUT.zarr
```

### Arguments
- `--forecast` — forecast Zarr.
- `--obs` — observation Zarr on the forecast's grid spacing (or the same stations).
- `--metric` — `hits` (event classification), `bias` (forecast − obs), `mae` (absolute error), `rmse` (root mean square error over `--reduce` dims), `crps` (ensemble continuous ranked probability score), or `brier` (squared error of ensemble event probability). Default: `hits`.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable (names may differ).
- `--threshold` — cutoff in the forecast's units. `hits` uses value ≥ threshold and defaults to `1`; `brier` uses value > threshold and requires an explicit cutoff. Ignored (with a stderr note) for other metrics.
- `--reduce` — dimension to average the score over; repeat for several. Required for `rmse`, which squares errors, averages, then takes the root. Refused for categorical `hits` and for the ensemble dimension. Missing pairs are excluded from the reduction.
- `--crps-estimator` — `standard` (default, spread denominator M²) or `fair` (M(M−1), requires at least two members). The sorted-member calculation avoids a pairwise M²-sized array.
- `--output`, `-o` — output Zarr.

### Output
- One float32 variable: `event_hit`, `bias`, `mae`, `rmse`, `crps`, or `brier_score`. `event_hit` is `1` hit (both ≥ threshold), `-1` disagree (only one is), `0` both below, with CF `flag_values` / `flag_meanings`.
- Error metrics use the forecast's difference units (a temperature error is `delta_degC`); Brier is dimensionless.
- Metric attrs record the CRPS estimator, ensemble size, reduction dims, and any ensemble averaging before deterministic scoring. Brier records its threshold and strict `>` comparison.
- NaN in either input stays NaN and is left out of the score.
- Stdout prints hit rate = hits / observed events, or the mean of the output metric (cos-latitude-weighted if latitude remains, otherwise plain). Also stamped as `verify_score_summary`. For reduced RMSE this is the mean of the remaining RMSE field, not a pooled RMSE over the original domain.

### Notes
- Grid spacing must match: a different lat or lon spacing exits 2. Put the obs on the forecast grid (for fine obs, an area mean such as `coarsen --reference-grid FC.zarr --method conservative`), not the forecast on the obs grid.
- Inputs keep only shared labels (inner join); no overlap on a dim exits 2 and prints both ranges. Single-time inputs must carry the same date.
- A forecast `step` dim exits 2 unless the obs have `step`: convert leads to valid times first.
- `hits`, `bias`, `mae` and `rmse` score the ensemble mean; `crps` and `brier` retain forecast members and require an ensemble dimension (`number`, `member`, or `realization`). A missing forecast member makes that score cell NaN. An observation ensemble is averaged first, with the same complete-member rule.
- Obs are converted to the forecast's units. Incompatible units (an `mm` total against an `mm day-1` rate), or units on only one input, exit 2. For a precip threshold in mm, give both inputs as period totals.
- Stations: sample the grid at the station points first, then pass the sample and the station Zarr. Pairs join on `station_id` and `time`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast s2s_kenya_week2_mm.zarr \
  --obs chirps_kenya_week2_mm_on_s2s.zarr --metric hits -v precip --threshold 10 -o hits_week2.zarr
```
Writes week-2 hit / disagree / below cells for ≥ 10 mm weekly rain over Kenya and prints the hit rate.

For ensemble verification, use `--metric crps --reduce time`, or `--metric brier --threshold 10 --reduce time` for the probability of strictly more than 10 mm. Plotting consumers must support the selected metric; this change does not extend plotting skills.

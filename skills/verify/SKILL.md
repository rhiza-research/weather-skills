---
name: verify
description: "Score a forecast against observations on a shared grid or shared stations: per time (event hits, bias, MAE, Brier score, ensemble CRPS, quintile RPS), per cell over time (RMSE, spread-skill ratio, POD / FAR / frequency bias / ETS, correlation), or as reliability and ROC curves. Writes the metric as a Zarr and prints a regional score. Use for forecast skill maps, event and probabilistic verification."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.5"
  catalog-group: transforms
---

# verify

Compares a forecast with observations cell by cell (or station by station) and writes the metric.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast FC.zarr --obs OBS.zarr [--metric NAME] [-v NAME] [--threshold 1] -o OUT.zarr
```

### Arguments
- `--forecast` — forecast Zarr.
- `--obs` — observation Zarr on the forecast's grid spacing (or the same stations).
- `--metric` — one of the metrics below. Default: `hits`.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable (names may differ).
- `--threshold` — event cutoff for `hits`, `brier`, `contingency`, `reliability` and `roc`, in the forecast's units: an event is value ≥ threshold. Default: `1`. Ignored (with a stderr note) for other metrics.
- `--output`, `-o` — output Zarr.

### Metrics

| `--metric` | Output | Scores | Stdout summary |
|---|---|---|---|
| `hits` | `event_hit` per time | ensemble mean | hit rate |
| `bias` | `bias` per time | ensemble mean | mean bias |
| `mae` | `mae` per time | ensemble mean | MAE |
| `brier` | `brier` per time | member fraction ≥ threshold | Brier and BSS vs the observed base rate |
| `crps` | `crps` per time | every member | CRPS |
| `crps_fair` | `crps_fair` per time | every member | fair CRPS |
| `rps` | `rps` per time | quintile probabilities | RPS and RPSS vs climatology |
| `rmse` | `rmse` per cell | ensemble mean | RMSE |
| `spread` | `spread_skill` per cell | every member | spread / skill ratio |
| `contingency` | `pod`, `far`, `frequency_bias`, `ets` per cell | ensemble mean | all four, pooled |
| `corr` | `corr` per cell | ensemble mean | mean correlation |
| `reliability` | curve on `probability_bin` | member fraction ≥ threshold | reliability, resolution, uncertainty |
| `roc` | curve on `probability_threshold` | member fraction ≥ threshold | ROC area |

### Output
- Per-time metrics keep the input dims (time, grid or stations), float32. Per-cell metrics collapse `time`. Curves pool every cell and time, cos-latitude weighted (equal weights on stations).
- `event_hit` is `1` hit (both ≥ threshold), `-1` disagree (only one is), `0` both below, with CF `flag_values` / `flag_meanings`.
- `bias`, `mae`, `crps`, `crps_fair` and `rmse` are in the forecast's units (a temperature error is `delta_degC`); the others are unitless.
- `brier` is (p − o)², where p is the fraction of members ≥ `--threshold` (0 or 1 without members) and o is 1 when the obs are ≥ it. BSS compares with always forecasting the observed base rate.
- `crps` is E|X − y| − ½E|X − X′| over the members; for a single forecast it equals the absolute error. Compare two forecasts' CRPS for a CRPSS.
- `crps_fair` averages |X − X′| over the m(m−1) distinct member pairs instead of all m², an unbiased estimate of the score an infinitely large ensemble would get (Ferro 2014), so ensembles of different sizes compare fairly. It needs at least 2 members.
- `rps` is the squared error between the forecast and observed quintile CDFs, summed over the four inner edges. RPSS compares with climatology (0.2 for each quintile).
- `spread_skill` is √(mean ensemble variance × (m+1)/m) / RMSE of the ensemble mean, for m members: 1 is calibrated, below 1 overconfident, above 1 underconfident.
- `contingency` counts hits, false alarms and misses over time: POD = hits / observed events, FAR = false alarms / forecast events, frequency bias = forecast events / observed events, ETS = equitable threat score (0 no skill, 1 perfect).
- `corr` is the Pearson correlation over time at each cell, NaN with fewer than 3 times. Pass anomalies (from `difference` or `standardize-anomaly`) on both inputs for the anomaly correlation (ACC).
- `reliability` has `forecast_probability`, `observed_frequency` and `sample_fraction` in ten bins of width 0.1; plot observed against forecast probability for a reliability diagram. Stdout gives the Brier score decomposition.
- `roc` has `hit_rate` and `false_alarm_rate` for warning at probability ≥ 0.1, 0.2, … 1.0; the area under the curve is the dataset attr `roc_area`.
- NaN in either input stays NaN and is left out of the score. Stdout prints the summary, also stamped as `verify_score_summary`.

### Notes
- Grid spacing must match: a different lat or lon spacing exits 2. Put the obs on the forecast grid (for fine obs, an area mean such as `coarsen --reference-grid FC.zarr --method conservative`), not the forecast on the obs grid.
- Inputs keep only shared labels (inner join); no overlap on a dim exits 2 and prints both ranges. Single-time inputs must carry the same date.
- A forecast `step` dim exits 2 unless the obs have `step`: convert leads to valid times first. For per-cell metrics and curves, stack many forecasts on `time` (one valid date each) so there is a sample to pool.
- A NaN member makes that cell NaN. `spread` needs an ensemble (`number`).
- Obs are converted to the forecast's units. Incompatible units (an `mm` total against an `mm day-1` rate), or units on only one input, exit 2. For a precip threshold in mm, give both inputs as period totals.
- `rps` needs a forecast of quintile probabilities with a `quintile_threshold` coord, as `pbc-correct` writes (`--components raw` puts a raw ensemble on the same quintiles). The obs are amounts on the same `time` labels: window means labeled at the window start (`aggregate-temporal --window 7 --align left`).
- Stations: sample the grid at the station points first, then pass the sample and the station Zarr. Pairs join on `station_id` and `time`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py --forecast s2s_kenya_week2_mm.zarr \
  --obs chirps_kenya_week2_mm_on_s2s.zarr --metric hits -v precip --threshold 10 -o hits_week2.zarr
```
Writes week-2 hit / disagree / below cells for ≥ 10 mm weekly rain over Kenya and prints the hit rate.

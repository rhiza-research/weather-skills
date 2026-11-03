---
name: online-ensemble
description: "Combine several expert forecasts (models) into one weighted ensemble whose weights are learned round by round from past errors, using PoolD's online learning under delayed observations (DORM+, DORM, AdaHedgeD, DUB) with optimistic hints. Writes the ensemble forecast, the weight of each model for each round, and each round's RMSE. Use to blend forecasts issued on a regular schedule, such as weekly subseasonal forecasts, when a forecast's obs only arrive several rounds after it is issued."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/online_ensemble.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# online-ensemble

Treats each forecast time as one round. For each round it picks expert weights from the losses already observed, then waits `--delay` rounds before that round's obs score it. Runs the [PoolD](https://github.com/geflaspohler/poold) learners of Flaspohler et al., *Online Learning with Optimism and Delay* (ICML 2021). They need no tuning.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/online_ensemble.py --forecast FC.zarr --obs OBS.zarr --delay D [--learner dormplus|dorm|adahedged|dub] [--hint recent_g|mean_g|none] [-v NAME] [--model-dim model] [--state-in STATE.json] [--state-out STATE.json] -o OUT.zarr
```

### Arguments
- `--forecast` — the expert forecasts stacked along a model dim, with a `time` axis of target (valid) dates, one per round, strictly increasing.
- `--obs` — observations on the forecast's grid (or the same stations), labeled with the same target dates.
- `--delay` — rounds between issuing a forecast and having all of its obs, at least 1. For forecasts issued every `S` days for a window that starts `L` days after issue and lasts `W` days, `delay = ceil((L + W + latency) / S)`, where latency is how long the obs take to publish. Weekly week-3–4 forecasts (`L` 14, `W` 14, `S` 7) take 4; daily inits scored on day 5 (`L` 5, `W` 1, `S` 1) take 6, or 7 with obs that publish the next day (IMERG Late).
- `--learner` — `dormplus` (DORM+, delayed optimistic regret matching+), `dorm`, `adahedged` (AdaHedgeD), or `dub` (AdaHedgeD with the delayed upper-bound step size). Default: `dormplus`. DORM+ weights move quickly and can put all weight on one model for a round; AdaHedgeD moves more smoothly. `dub` learns slowest.
- `--hint` — guess at the losses still waiting on obs: `recent_g` (the most recent observed loss gradient), `mean_g` (the mean of all observed gradients), or `none`. Default: `recent_g`.
- `--variable`, `-v` — variable name in both inputs. Default: each input's first usable variable (names may differ).
- `--model-dim` — dim of `--forecast` holding the experts. Default: `model`.
- `--state-in` — a state file from an earlier run's `--state-out`. The run resumes the learner where that run stopped, instead of starting again from uniform weights.
- `--state-out` — write the learner's state after the last round to this JSON file.
- `--output`, `-o` — output Zarr.

### Output
- The forecast variable: the weighted sum of the experts for each round, in the forecast's units. NaN where any expert is NaN.
- `weight(time, model)` — each expert's weight for each round, summing to 1. The weights for the newest rounds use only obs already available when those forecasts were issued.
- `expert_rmse(time, model)` and `ensemble_rmse(time)` — each round's RMSE over its cells, cos-latitude weighted on grids. NaN on rounds without obs.
- Stdout prints the mean RMSE of the ensemble and each expert over the scored rounds, the mean regret (ensemble mean RMSE minus the best single expert's; negative means the blend beat every expert), and the newest round's weights. The same text is stamped as `online_ensemble_summary`.

### Notes
- Stack experts first. For each model: `aggregate-temporal --time-dim step` to the round's window (e.g. `--period daily`), `select --dim step` the same lead, `step-to-time`, and `summarize-dim --dim number --method mean` when the models have different member counts. Then `coarsen` them onto one grid and `concat --dim model --coords a,b,c`. A forecast `step` dim exits 2.
- `concat` may sort the `model` labels, so read weights by label, not by input order.
- Rounds are counted by position along `time`, so space them evenly.
- The loss of a round is the RMSE over the cells where the obs and every expert are finite. An expert with no data in a round exits 2.
- Rounds after the last obs are forecast but not scored: the last `delay` rounds are the live forecast. Obs missing for a round before the last scored round exits 2, since the learner can never get that round's feedback.
- Ensemble `number` is averaged first. Obs are converted to the forecast's units; incompatible units exit 2.
- Weights start uniform and stay uniform until the first feedback arrives, `delay` rounds in.

### Resuming from a state file
- The state file holds everything the learner needs to continue: its internal state, the rounds still waiting on obs and the weights they were given, and the gradients the hint is built from. A resumed run gives exactly the weights one run over the whole record would.
- `--forecast` must include the rounds the state is still waiting on (the last `delay` rounds of the earlier run) and the new rounds after them. Passing the whole archive again is fine: rounds the earlier run already finished are skipped, with a note on stderr. A waiting round missing from `--forecast` exits 2.
- `--obs` needs the obs for the waiting rounds and the new ones, as in a single run.
- The new rounds must follow on from the earlier run at the same spacing, since `--delay` counts rounds across runs.
- `--learner`, `--hint`, `--delay` and the expert labels (and their order) must match the earlier run; a mismatch exits 2.
- The output covers the waiting rounds and the new ones; the waiting rounds keep the weights the earlier run gave them. The stdout means cover only these rounds.
- Pass `--state-in` and `--state-out` together to roll forward each time new forecasts arrive.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/online_ensemble.py --forecast wk34_models_kenya.zarr \
  --obs chirps_wk34_kenya_on_models.zarr -v precip --delay 4 -o wk34_online_ensemble.zarr
```
Learns DORM+ weights for weekly week-3–4 rainfall forecasts from three models over Kenya, scored against CHIRPS, and writes the blended forecast with each week's weights.
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/online_ensemble.py --forecast wk34_models_kenya_new.zarr \
  --obs chirps_wk34_kenya_on_models.zarr -v precip --delay 4 \
  --state-in wk34_state.json --state-out wk34_state.json -o wk34_online_ensemble_new.zarr
```
Next week: resumes from last week's state with the new forecasts and newly published obs, and writes the updated state back to the same file.

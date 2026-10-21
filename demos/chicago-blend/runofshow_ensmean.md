# Run of show: the Chicago-method forecast blend, in under 5 minutes

For Claude, on the demo laptop. Follow the steps in order and run every command as written.

What it does:
- Downloads a small data pack: 20 years of past forecasts plus 45 years of observed rainfall over Kenya.
- Learns how much to trust each forecast model, one logistic regression per lead week (the University of Chicago "blending" method).
- Tests itself on seasons it never saw, then blends this season's live forecasts.
- Writes a report with maps.

It does this for two events, to show the blend works for any event: the start of the rainy season, and a wet week (at least 25 mm in 7 days).

Total time: about 1–2 minutes to install, plus about 2 minutes to run.

## What you need
- **A Mac or Linux machine** with `git` and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`). `uv` installs Python itself.
- **The demo service-account key**, a JSON file shared by Rhiza Research, which can read `gs://sheerwater-datalake/gates-demo/`. Never print it or commit it.

## 1. Install the skills (about 1–2 min the first time)
```bash
mkdir -p ~/chicago-demo && cd ~/chicago-demo
git clone -q -b gates-demo https://github.com/rhiza-research/weather-skills
git clone -q https://github.com/rhiza-research/weather-skills-plotting
(cd weather-skills && uv sync -q --group dev)
```
- **`weather-skills`** (branch `gates-demo`) holds the skills: `mme-fit` and `mme-blend` (the blend), `indicator` (turns a rainfall rule into a yes/no event), and the rest of the toolbox. It also holds this demo in `demos/chicago-blend/`.
- **`weather-skills-plotting`** holds the `plot` skill for the maps.

## 2. Point at the key and the folders
```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/gates-demo-key.json
export SKILLS=~/chicago-demo/weather-skills/skills
export HERE=~/chicago-demo/weather-skills/demos/chicago-blend/scripts
export PLOT=~/chicago-demo/weather-skills-plotting/skills/plot/scripts/plot.py
export OUT=~/chicago-demo/results
py() { uv run --project "$SKILLS/.." python -I "$@"; }    # Python with every skill's dependencies
```

## 3. Run the blend (about 2 min)
```bash
bash $HERE/run_demo.sh
```
It prints each step and its time:
1. **Data:** downloads the 3 MB demo pack to `~/chicago-demo/demo-pack` (skipped if already there).
2. **Observed events:** builds, from CHIRPS satellite rainfall, the record of when each event happened in every season since 1981 (`indicator`).
3. **Cross-validation:** hides one season at a time, fits the blend on the rest (`mme-fit`), predicts the hidden season (`mme-blend`) and scores it.
4. **Fit + blend 2026:** fits on all seasons and blends the 15 October 2026 forecasts.
5. **Maps + report.**

It runs this for `onset`, then for `wetweek`.

## 4. Read and report the results
Open `$OUT/onset/report.md` and `$OUT/wetweek/report.md` (each with two maps next to it). Report back in this order:

1. **Does the blend work?** Read the "How well does it work?" table. It shows the Brier skill score against always forecasting the historical rate: above 0 means better, and 0.10 means 10% better. On the run of 21 October 2026, ECMWF + GEFS over 20 seasons scored:

   | Event | Week 1 | Week 2 | Week 3 | Week 4 |
   |---|---|---|---|---|
   | Onset | +0.12 | +0.10 | +0.08 | +0.09 |
   | Wet week | +0.43 | +0.30 | +0.20 | +0.19 |

2. **Which models matter?** Read the weights table. Bigger coefficients mean the model's rainfall forecast moves the probability more. ECMWF carries more weight than GEFS. Over 2016–2021, adding Cumulus to ECMWF is best one to two weeks ahead.
3. **What does it say for this season?** Read the 2026 outlook table and maps. Each number is the chance the event happens in that week, given it hasn't happened before.

   On 21 October 2026, onset ran 0.09–0.15 a week for ECMWF + GEFS, highest around Lake Victoria first, then central and south-eastern Kenya. That chains to roughly a 4-in-10 chance by mid-November.
4. **Caveats** (always mention them):
   - Cumulus has only 6 seasons to test on, and its training years aren't confirmed yet.
   - The live ECMWF model is newer than its hindcasts.
   - Cells where the rains may already have started aren't masked.

## Any event
The blend doesn't care what the event is. Set a rule and run again, for example:
```bash
W=~/chicago-demo/run/onset/main                                   # any working folder run_demo.sh made
rm -rf $W/chirps/ev_*.zarr                                         # forget the old events
EVENT_RULE="precip sum 7d <= 5" EVENT_FIRST_IN= py $HERE/events.py $W   # a dry week
py $HERE/clim.py $W && py $HERE/cv.py $W                           # its climatology and skill
```

`indicator` rules combine rolling sums, counts and dry or wet spells. Some examples:
- `"precip sum 3d >= 20"` (heavy rain);
- `"not precip consecutive-below 1 7d within 21d"` (no 7-day dry spell);
- the aliases `icpac-onset` and `chc-onset`.

To change the events `run_demo.sh` runs, edit the `case` block at the top of its loop. `EVENT_FIRST_IN=7d` means "the first time this season"; leave it empty for "any week".

To fit on station rain gauges (TAHMO) instead of CHIRPS, see `RUNBOOK.md`. The `mme-fit` and `mme-blend` skills accept station data directly; sample the forecasts at the stations with `point-value`.

## Going further (not needed for the demo)
- **Full archive** (about 25 GB, East Africa) in `gs://sheerwater-datalake/gates-demo/archive/`:
  - ECMWF reforecast 1996–2021;
  - GEFS reforecast 1999–2019 plus operational GEFS 2020–2026;
  - Cumulus hindcasts 2016–2024;
  - daily CHIRPS 1981–2026;
  - TAHMO stations 2015–2026.

  Download it with `py $HERE/download_pack.py gs://sheerwater-datalake/gates-demo/archive ~/chicago-demo/archive`. Then follow `RUNBOOK.md` to rebuild everything from raw data.
- **Fresh forecasts:** `3_forecasts_2026.sh` (ECMWF, GEFS) and `3b_cumulus_2026.sh` (Cumulus) fetch a newer init. Cumulus needs `AZURE_STORAGE_SAS_TOKEN`.

## If something goes wrong
- **`403` / `Forbidden` on download:** `GOOGLE_APPLICATION_CREDENTIALS` is unset or points at the wrong file.
- **`uv: command not found`:** install `uv` (see "What you need") and open a new shell.
- **A step fails:** rerun `run_demo.sh`; it rebuilds its working folder each time. For details, run the failing step's command from the script by hand without `>/dev/null`.

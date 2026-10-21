# Run of show: will the rains start this week? A blended onset forecast in under 5 minutes

For Claude, on the demo laptop. Follow the steps in order and run every command as written.

What it does:
- Learns, from 20 years of past forecasts and 45 years of satellite rainfall, how much to trust each forecast model's ensemble. It fits one logistic regression per lead week, the University of Chicago "blending" method.
- Tests itself on seasons it never saw.
- Blends this season's live forecasts (ECMWF and the Cumulus AI model) into the chance that the rainy season starts in each of the next two weeks.
- Reports results for Kenya and Uganda separately, with maps.

Total time:
- before the meeting: about 2 minutes to install and 1 minute to download;
- during the demo: about **2 minutes** to run.

## What you need
- **A Mac or Linux machine** with `git` and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`). `uv` installs Python itself.
- **About 3.5 GB of free disk:** 2.2 GB of data, 0.9 GB of Python packages and 0.4 GB of working files.
- **The demo service-account key:** a JSON file shared by Rhiza Research, which can read `gs://sheerwater-datalake/gates-demo/`. Never print it or commit it.

## Before the meeting

### 1. Install the skills (about 1–2 min)
```bash
mkdir -p ~/chicago-demo && cd ~/chicago-demo
git clone -q -b gates-demo https://github.com/rhiza-research/weather-skills
git clone -q https://github.com/rhiza-research/weather-skills-plotting
(cd weather-skills && uv sync -q --group dev)
```
- **`weather-skills`** (branch `gates-demo`) holds the skills, and this demo in `demos/chicago-blend/`:
  - `mme-fit` and `mme-blend`: the blend;
  - `indicator`: turns a rainfall rule into a yes/no event, and an ensemble into a probability;
  - the rest of the toolbox.
- **`weather-skills-plotting`** holds the `plot` skill for the maps.

### 2. Point at the key and the folders
Run these in every new shell:
```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/gates-demo-key.json
export SKILLS=~/chicago-demo/weather-skills/skills
export HERE=~/chicago-demo/weather-skills/demos/chicago-blend/scripts
export PLOT=~/chicago-demo/weather-skills-plotting/skills/plot/scripts/plot.py
export ARCHIVE=~/chicago-demo/archive
export RUN=~/chicago-demo/run
export OUT=~/chicago-demo/results
export INIT=2026-10-15
py() { uv run --project "$SKILLS/.." python -I "$@"; }    # Python with every skill's dependencies
```

### 3. Download the inputs (2.1 GB, about 40 s on a fast connection)
```bash
B=gs://sheerwater-datalake/gates-demo/archive
for f in chirps_daily_1p5.zarr ecmwf_reforecast.zarr cumulus_hindcast.zarr \
         live/ecmwf/ecmwf_$INIT.zarr live/cumulus/cumulus_$INIT.zarr; do
  mkdir -p "$(dirname "$ARCHIVE/$f")"
  py $HERE/download_pack.py $B/$f $ARCHIVE/$f
done
```
What it downloads:
- CHIRPS satellite rainfall since 1981 on the 1.5° cells;
- the ECMWF reforecast: 10 members, 20 past years;
- the Cumulus AI hindcast: 30 members, 2016–2024;
- the two live forecasts for `$INIT`.

Anything already downloaded is skipped.

### 4. Rehearse once
Run step 5 below and check that the report and maps appear.

## During the demo

### 5. Run the blend (about 2 min)
```bash
bash $HERE/run_demo.sh
```
It prints each stage and its time:
1. **Observed onsets:** finds when the rains really started in every season since 1981 (`indicator --rule icpac-onset`).
2. **Member predictors:** for each past and live forecast, the share of ensemble members that start the rains in each week (`indicator --probability`).
3. **Cross-validation:** hides one season at a time, fits the blend on the rest (`mme-fit`), predicts the hidden season (`mme-blend`) and scores it.
4. **Fit + blend 2026:** fits on all seasons and blends the `$INIT` forecasts.
5. **Maps + report.**

### 6. Read and report the results
Open `$OUT/report.md`, with the maps next to it:
- `map_ecmwf+cumulus.png`: all cells;
- `map_ecmwf+cumulus_Kenya.png`;
- `map_ecmwf+cumulus_Uganda.png`;
- `map_cumulus.png` and `map_ecmwf.png`.

Report back in this order:

1. **Does the blend work?** Read the "How well does it work?" tables: Brier skill score against climatology, where above 0 is better and 0.30 means 30% better. On the run of 21 October 2026 (6 test seasons, 2016–2021), weeks 1 / 2:

   | Predictors | Kenya | Uganda |
   |---|---|---|
   | ECMWF | +0.11 / +0.06 | +0.10 / −0.04 |
   | Cumulus | +0.31 / +0.20 | +0.29 / +0.16 |
   | ECMWF + Cumulus | **+0.33 / +0.20** | **+0.31 / +0.15** |

2. **Which models matter?** Read the weights table. In the ECMWF + Cumulus blend, Cumulus carries most of the weight (5.8 vs 1.7 in week 1). The AI model adds the most skill.
3. **What does it say for this season?** Read the 2026 outlook tables and the country maps. Each number is the chance the rains start in that week, if they haven't already. On the 15 October init:
   - **Uganda:** about **60%** for 15–21 October, against 39% in an average year. The rains are starting now across most of the country.
   - **Kenya:** about **8% then 11%**, close to an average year. The highest chances are on the western border first, then in the east and north-east.
4. **Caveats** (always mention them):
   - Cumulus has only 6 seasons to test on.
   - The live ECMWF has 101 members, but its training years had 10.
   - Places where the rains have already started aren't masked.
   - Only two weeks ahead, because the onset definition looks 27 days past the first rains.

## Any event
The blend doesn't care what the event is. `indicator` rules combine rolling sums, counts and dry or wet spells:
- `"precip sum 7d >= 25"`: a wet week;
- `"not precip consecutive-below 1 7d within 21d"`: no 7-day dry spell;
- the aliases `icpac-onset` and `chc-onset`.

To change the event, edit `--rule` in `members_events.py`, `members_predictors.py` and `members_live.py`, then rerun `run_demo.sh`. For a rule that doesn't look ahead, such as a wet week, weeks 3–6 can be added in `WEEKS`.

To fit on TAHMO rain gauges instead of CHIRPS: the full archive has them (see below). `mme-fit` and `mme-blend` accept station data directly; sample the forecasts at the stations with `point-value`.

## Going further (not needed for the demo)
- **Full archive** (4.4 GB, Ethiopia + Kenya + Uganda) in `gs://sheerwater-datalake/gates-demo/archive/`:
  - ECMWF reforecast 1996–2021 (10 members) and real-time 2016–2022 (50 members);
  - GEFS reforecast 1999–2019 (11 members), plus operational GEFS 2020–2025 being added;
  - Cumulus hindcasts 2016–2024 (30 members);
  - daily CHIRPS 1981–2026;
  - TAHMO stations 2015–2026;
  - live ECMWF, GEFS and Cumulus forecasts since 1 September 2026.

  Get it with `gcloud storage rsync -r gs://sheerwater-datalake/gates-demo/archive $ARCHIVE`. `RUNBOOK.md` describes every store and step.
- **Another init:** set `INIT` to any date with both `live/ecmwf/ecmwf_$INIT.zarr` and `live/cumulus/cumulus_$INIT.zarr` in the archive, download those two (step 3), and rerun.

## If something goes wrong
- **`403` / `Forbidden` on download:** `GOOGLE_APPLICATION_CREDENTIALS` is unset or points at the wrong file.
- **`uv: command not found`:** install `uv` (see "What you need") and open a new shell.
- **A stage fails:** rerun `run_demo.sh`; it rebuilds `$RUN` each time. For details, run the failing stage's command from the script by hand without `>/dev/null`.

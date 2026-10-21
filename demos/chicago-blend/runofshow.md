# Run of show: have the rains started? A blended onset forecast in under 5 minutes

For Claude, on the demo laptop. Follow the steps in order and run every command as written.

What it does:
- Learns, from 20 years of past forecasts and 45 years of satellite rainfall, how much to trust each forecast model's ensemble. It fits one logistic regression for each horizon (7 and 14 days ahead), the University of Chicago "blending" method.
- Tests itself on seasons it never saw.
- Blends this season's live forecasts (ECMWF and the Cumulus AI model) into the chance that the rainy season has started within one and two weeks. Where satellite rainfall already shows the rains started, the chance is 1.
- Reports results for Kenya and Uganda separately, with maps.

Total time:
- before the meeting: about 2 minutes to install and 1 minute to download;
- during the demo: about **2½ minutes** to run.

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
for f in chirps_daily_1p5.zarr ecmwf_reforecast.zarr cumulus_hindcast.zarr live/chirps_2026.zarr \
         live/ecmwf/ecmwf_$INIT.zarr live/cumulus/cumulus_$INIT.zarr; do
  mkdir -p "$(dirname "$ARCHIVE/$f")"
  py $HERE/download_pack.py $B/$f $ARCHIVE/$f
done
```
What it downloads:
- CHIRPS satellite rainfall since 1981 on the 1.5° cells;
- the ECMWF reforecast: 10 members, 20 past years;
- the Cumulus AI hindcast: 30 members, 2016–2024;
- this season's CHIRPS up to mid-October, to see where the rains have already started;
- the two live forecasts for `$INIT`.

Anything already downloaded is skipped.

### 4. Rehearse once
Run step 5 below and check that the report and maps appear.

## During the demo

### 5. Run the blend (about 2½ min)
```bash
bash $HERE/run_demo.sh
```
It prints each stage and its time:
1. **Observed onsets:** finds when the rains really started in every season since 1981 (`indicator --rule icpac-onset`).
2. **Member predictors:** for each past and live forecast, the share of ensemble members that start the rains within 7 and within 14 days (`indicator --probability`).
3. **Cross-validation:** hides one season at a time, fits the blend on the rest (`mme-fit`), predicts the hidden season (`mme-blend`) and scores it.
4. **Fit + blend 2026:** fits on all seasons, blends the `$INIT` forecasts, and sets places where CHIRPS already shows the onset to 1.
5. **Maps + report.**

### 6. Read and report the results
Open `$OUT/report.md`, with the maps next to it:
- `map_ecmwf+cumulus.png`: all cells;
- `map_ecmwf+cumulus_Kenya.png`;
- `map_ecmwf+cumulus_Uganda.png`;
- `map_cumulus.png` and `map_ecmwf.png`.

Report back in this order:

1. **Does the blend work?** Read the "How well does it work?" tables: Brier skill score against climatology, where above 0 is better and 0.30 means 30% better. On the run of 21 October 2026 (6 test seasons, 2016–2021), within 7 / 14 days:

   | Predictors | Kenya | Uganda |
   |---|---|---|
   | ECMWF | +0.11 / +0.07 | +0.10 / +0.04 |
   | Cumulus | +0.31 / +0.27 | +0.29 / +0.35 |
   | ECMWF + Cumulus | **+0.33 / +0.29** | **+0.31 / +0.37** |

2. **Which models matter?** Read the weights table. In the ECMWF + Cumulus blend, Cumulus carries most of the weight (5.8 vs 1.7 for 7 days). The AI model adds the most skill.
3. **What does it say for this season?** Read the 2026 outlook tables and the country maps. Each number is the chance the rains have started by that date. Where CHIRPS already shows the onset, it's 1. On the 15 October init:
   - **Uganda:** the rains have already started in 9 of its 17 cells, mostly the north and west. Around Lake Victoria the blend gives high chances by 21 October. Area mean: about **76%** by 21 October and **80%** by 28 October.
   - **Kenya:** no onset yet. About **8%** by 21 October and **19%** by 28 October, close to an average year. The highest chances are near Lake Victoria and in the east and north-east.
4. **Caveats** (always mention them):
   - Cumulus has only 6 seasons to test on.
   - The live ECMWF has 101 members, but its training years had 10.
   - CHIRPS confirms an onset only 3 weeks after the first rains, so the most recent starts aren't marked yet.
   - Only two weeks ahead, because the onset definition looks 27 days past the first rains.

## Any event
The blend doesn't care what the event is. `indicator` rules combine rolling sums, counts and dry or wet spells:
- `"precip sum 7d >= 25"`: a wet week;
- `"not precip consecutive-below 1 7d within 21d"`: no 7-day dry spell;
- the aliases `icpac-onset` and `chc-onset`.

To change the event, edit `--rule` in `members_events.py`, `members_predictors.py` and `members_live.py`, then rerun `run_demo.sh`. For a rule that doesn't look ahead, such as a wet week, longer windows can be added in `WINDOWS`.

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
- **Another init:** set `INIT` to any date with both `live/ecmwf/ecmwf_$INIT.zarr` and `live/cumulus/cumulus_$INIT.zarr` in the archive, download those two (step 3), and rerun. For an init after 15 October, refresh `live/chirps_2026.zarr` first (see `RUNBOOK.md`).

## If something goes wrong
- **`403` / `Forbidden` on download:** `GOOGLE_APPLICATION_CREDENTIALS` is unset or points at the wrong file.
- **`uv: command not found`:** install `uv` (see "What you need") and open a new shell.
- **A stage fails:** rerun `run_demo.sh`; it rebuilds `$RUN` each time. For details, run the failing stage's command from the script by hand without `>/dev/null`.

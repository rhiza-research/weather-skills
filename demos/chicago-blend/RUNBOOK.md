# Runbook: OND 2026 onset forecast for East Africa from ensemble members (mme-fit / mme-blend)

This file tells an agent how to rerun the analysis end to end. It fits one logistic regression per lead week that turns each model's **ensemble members** into a calibrated probability that the rainy season starts in that week. It checks skill by leaving one season out at a time, then blends the live 2026 forecasts. Results are reported for the whole box and for Kenya and Uganda separately.

Every script lives in `scripts/` next to this file. The previous ensemble-mean version of this analysis is in `RUNBOOK_ensmean.md`, `run_demo_ensmean.sh` and `report_ensmean.py`.

Never fill missing observations: the skills leave a missing sample out, and so does every script here.

## What it computes
- **Event:** the ICPAC rainy-season onset (`icpac-onset`: a 3-day total ≥ 20 mm with no 7-day dry spell, under 1 mm a day, in the next 21 days).
- **Target:** for lead week *k* after an init, does the season's **first onset on or after 15 September** fall in that week?
  - 1 if yes;
  - 0 if not yet;
  - NaN once the onset has already happened (dropped from fitting and scoring).

  Built with `indicator --rule icpac-onset --first-in 7d` on each season of CHIRPS, 15 Sep – 31 Jan.
- **Predictors:** for each model, the **share of its ensemble members** whose first onset since 15 Sep (by valid date) falls in the week. Built with `indicator --rule icpac-onset --first-in 7d --season-start 09-15 --probability --time-dim step` on the members, read at lead day 0 (week 1) and 7 (week 2). Members already past onset count as 0.
- **Leads:** weeks 1–2 only. The onset rule looks 27 days past the trigger, so a 46-day forecast can decide it only through about day 19.
- **Model:** `mme-fit --model logistic --fit-along step`. One unpenalized logistic regression per lead week, pooled over cells and seasons.
- **Region:** the 1.5° cells centred 6°S – 7.5°N, 28.5°E – 48°E (10 × 14). Northern Ethiopia, whose main rains are June – September, is left out. Kenya and Uganda are each scored on every cell that touches the country (`clip-region --region`), so border cells count for both: 36 cells for Kenya, 17 for Uganda.

## Quick run (from the local archive)
```bash
export SKILLS=/path/to/weather-skills/skills          # branch gates-demo
export PLOT=/path/to/weather-skills-plotting/skills/plot/scripts/plot.py
export HERE=/path/to/ond2026-onset/scripts
export ARCHIVE=/path/to/archive                       # a copy of gs://sheerwater-datalake/gates-demo/archive
export RUN=/path/to/scratch/run                       # working files, about 370 MB; rebuilt every run
export OUT=/path/to/results                           # report.md and the maps
export INIT=2026-10-15                                # the live init to blend
bash $HERE/run_demo.sh
```
The run only needs these parts of the archive (2.1 GB):
- `chirps_daily_1p5.zarr`;
- `ecmwf_reforecast.zarr`;
- `cumulus_hindcast.zarr`;
- `live/ecmwf/ecmwf_$INIT.zarr`;
- `live/cumulus/cumulus_$INIT.zarr`.

Timings on 21 Oct 2026 (Apple silicon, an archive holding only those five inputs, downloaded with `download_pack.py` in 36 s):

| Stage | Script(s) | Time |
|---|---|---|
| Observed onsets | `members_events.py` | 15 s |
| Member predictors, hindcast and live | `members_predictors.py ... 2022 cumulus`, `members_live.py` | 43 s |
| Cross-validation | `clim.py`, `cv.py ... cumulus-no-gefs` | 13 s |
| Final fits and 2026 blends | `fit_blend.py ... ecmwf cumulus ecmwf+cumulus` | 2 s |
| Maps and report | `clip-region`, `plot` (five maps), `report.py` | 27 s |
| **Total** | | **100 s** |

The report was identical to a run on the full archive.

Every script calls the skills in-process through `scripts/skillrun.py`: the same code as `uv run <script>`, without a new process per call. Run the scripts with `uv run --project "$SKILLS/.." python -I` (define `py() { uv run --project "$SKILLS/.." python -I "$@"; }`; a `$PY` string is not split into words by zsh). `-I` keeps Python from importing from the current directory, because the scripts read downloaded data.

## The archive
`gs://sheerwater-datalake/gates-demo/archive/` (private; the demo service account can read it), about 4.4 GB, all cut to Ethiopia + Kenya + Uganda (15.75°N – 6.75°S, 27.75°E – 48.75°E) and September – December inits. Copy all of it with:
```bash
gcloud storage rsync -r gs://sheerwater-datalake/gates-demo/archive $ARCHIVE
```

| Store | Contents | Size |
|---|---|---|
| `ecmwf_reforecast.zarr` | WeatherBench2 IFS extended-range reforecast, **10 members**: 244 Sep – Dec issuances 2016 – 2022 (Mon/Thu) × 20 hindcast years each (1996 – 2021) × 46 daily leads, 1.5°. `total_precipitation_24hr` in metres per day (× 1000 for mm). The 2-D `time` coord holds the real init dates. | 830 MB |
| `ecmwf_realtime.zarr` | The same issuances' real-time forecasts, 50 members. | 203 MB |
| `cumulus_hindcast.zarr` | Cumulus (Aurora S2S) hindcast run `gratis-czajkowski`, the one matching the live run: 558 Sep – Dec inits 2016 – 2024 (every 2 days) × **30 members** × 46 daily steps, 1° IMERG-trained `precip` in mm day-1. | 1.3 GB |
| `gefs_reforecast.zarr` | Sheerwater GEFSv12 reforecast: 1073 Wednesday inits 1999 – 2019 × **11 members** × 35 daily leads, 1°. Open with `decode_times=False` (`init_time` in days since 1960-01-01). | 203 MB |
| `gefs_operational.zarr` | Operational GEFS, every Sep – Dec init 2020 – 2025, 31 members, daily means on the 1.5° cells. Being built on a cloud VM (`scripts/gefs_job/`); until it finishes only `gefs_operational.parts/` exists. | |
| `chirps_daily.zarr` | CHIRPS final, Sep – Jan of each season 1981 – 2026, daily, 0.05°, mm day-1. | 1.6 GB |
| `chirps_daily_1p5.zarr` | The same, coarsened conservatively onto the 1.5° cells of `live/grid_1p5.zarr` (15 × 14). | 3.5 MB |
| `tahmo/` | TAHMO daily station observations, Ethiopia, Kenya and Uganda, one file per year 2015 – 2026, plus `stations.csv`. | 10 MB |
| `live/ecmwf/` | Live ECMWF 46-day ensemble, 101 members, 1.5°, one store per init since 1 Sep 2026. | 86 MB |
| `live/cumulus/` | Live Cumulus (`utmost-plane`), 30 members, 1°, inits 3 – 21 Oct 2026. | 47 MB |
| `live/gefs/` | Live GEFS, 31 members, daily means at 1.5°, every init since 1 Sep 2026. | 64 MB |
| `live/grid_1p5.zarr` | The 1.5° target grid. | |

GEFS isn't in the current analysis yet. Its reforecast ends in 2019, so it overlaps Cumulus for only 4 seasons until the operational 2020 – 2025 store is finished.

How the archive was built (scripts in `scripts/`; rerun only to rebuild it):
- ECMWF reforecast and real-time: `cloud_members.py`, run on a Coiled VM next to the source.
- GEFS operational: `gefs_job/gefs_ops_skills.py` on a Coiled VM, skills only (`dynamical-fetch` → `aggregate-temporal --period daily` → `coarsen --method conservative`).
- CHIRPS, GEFS reforecast and TAHMO: `assemble_local.py`. Live forecasts: `assemble_live.py`.
- `chirps_daily_1p5.zarr`: `coarsen -i chirps_daily.zarr --reference-grid live/grid_1p5.zarr --method conservative`.

## Steps
What `run_demo.sh` does, for running or debugging one stage at a time (`$RUN` as the working folder):

### 1. Observed onsets
```bash
py $HERE/members_events.py $ARCHIVE $RUN
```
1. `clip-region` a live ECMWF init to the analysis box gives `grid.zarr`, the 10 × 14 target cells.
2. `clip-region` `chirps_daily_1p5.zarr` to the same box gives `chirps15.zarr`.
3. For each season 1981 – 2025, `select` 15 Sep – 31 Jan and run `indicator --rule icpac-onset --first-in 7d`. `--first-in` counts from the first date of its input, so it runs per season, never on a multi-season series.
4. `concat` the seasons into `obs_event.zarr`.

### 2. Member predictors
```bash
py $HERE/members_predictors.py $ARCHIVE $RUN 2022 cumulus
py $HERE/members_live.py $ARCHIVE $RUN $INIT ecmwf cumulus
```
- **ECMWF:** the 2022 model cycle's Thursday issuances (hindcast years 2002 – 2021), reshaped from WeatherBench2's issuance × hindcast-year layout to one init axis (own code, since no skill reads that layout). Then `clip-region` and `indicator`. This gives `ecmwf_hind.zarr`, 360 inits.
- **Cumulus:** for each ECMWF init, the latest Cumulus init on or before it, at most 1 day earlier. It is read at lead day lag + 0 and lag + 7 so the weeks line up by valid date, then put on the 1.5° cells with `coarsen --method conservative`. This gives `cumulus_hind.zarr`, 108 inits (2016 – 2021).
- **Live:** the same for `$INIT`, giving `live/ecmwf_wk.zarr` and `live/cumulus_wk.zarr`.

Cumulus's 1° grid doesn't fully cover the southern row and the eastern column of 1.5° cells, so those are NaN for Cumulus (about 16% of cells).

### 3. Climatology and cross-validation
```bash
EC_INIT=$INIT py $HERE/clim.py $RUN
py $HERE/cv.py $RUN cumulus-no-gefs > $RUN/cv.txt
```
- **`clim.py`** writes, for the same inits and weeks, the chance of the event on that day of the season:
  - `clim_hind.zarr`: every 1981 – 2025 season except the init's own;
  - `clim_2026_analog.zarr`: the 2026 El Niño analog years 1982 1994 1997 2006 2015 2019 2023;
  - `clim_2026_all.zarr`: all years.
- **`cv.py`:** for each of the 6 seasons ECMWF and Cumulus share (2016 – 2021), it fits on the other 5 with `mme-fit`, blends the held-out season with `mme-blend`, and scores it. The sets are `ecmwf`, `cumulus`, `ecmwf+cumulus` and `clim`. Every set is scored on the same cell-weeks: those where the observation and every input are present. Rows go to `cv_predictions_cumulus-no-gefs.csv`, with each sample's cell.

Other modes: `cv.py $RUN no-gefs` scores ECMWF alone and ECMWF + climatology over all 20 ECMWF seasons. The modes `cumulus` and the default include GEFS.

### 4. Final fits and 2026 blends
```bash
py $HERE/fit_blend.py $RUN ecmwf cumulus ecmwf+cumulus
```
It fits on all seasons and blends the live forecasts, writing `w_<set>.zarr` and `p2026_<set>.zarr`.

### 5. Maps and report
`run_demo.sh` plots `p2026_<set>.zarr` for all cells, one panel per lead week. It clips the cells (`cells_Kenya.zarr`, `cells_Uganda.zarr`) and the ECMWF + Cumulus blend to each country, plots those, and runs:
```bash
py $HERE/report.py $OUT/report.md $RUN
```
The report has the skill table, weights and 2026 outlook for all cells, Kenya and Uganda.

## Results (run of 21 October 2026, init 15 October 2026)
**Cross-validated Brier skill score against climatology**, weeks 1 / 2, 2016 – 2021 (6 seasons). Above 0 beats climatology.

| Predictors | All cells (415 onsets a week) | Kenya (118) | Uganda (93) |
|---|---|---|---|
| ECMWF | +0.04 / −0.06 | +0.11 / +0.06 | +0.10 / −0.04 |
| Cumulus | +0.22 / +0.07 | +0.31 / +0.20 | +0.29 / +0.16 |
| ECMWF + Cumulus | **+0.24 / +0.07** | **+0.33 / +0.20** | **+0.31 / +0.15** |

Over all 20 ECMWF seasons (2019 cycle, 1999 – 2018, `cv.py no-gefs`), the ECMWF + climatology set scored +0.07 / +0.02 and ECMWF alone +0.02 / −0.11.

**Weights** (log-odds per unit of members' onset share, fitted on all seasons):

| Blend | Week | Intercept | Coefficients |
|---|---|---|---|
| ECMWF | 1 / 2 | −3.50 / −3.06 | ECMWF 4.79 / 5.17 |
| Cumulus | 1 / 2 | −3.81 / −3.17 | Cumulus 7.12 / 6.79 |
| ECMWF + Cumulus | 1 / 2 | −3.97 / −3.19 | ECMWF 1.70 / 0.53, Cumulus 5.75 / 6.34 |

**2026 outlook**: the area-mean chance that the rains start in each week (15 – 21 Oct / 22 – 28 Oct).

| Forecast | All cells | Kenya | Uganda |
|---|---|---|---|
| ECMWF + Cumulus blend | 21% / 9% | 8% / 11% | 60% / 7% |
| Cumulus blend | 20% / 9% | 8% / 11% | 59% / 7% |
| ECMWF blend | 19% / 10% | 8% / 12% | 39% / 9% |
| Climatology, all years | 17% / 13% | 11% / 8% | 39% / 37% |

What the outlook shows:
- **Uganda:** the blends put the start of the rains in the week of 15 – 21 October in most of the country, well above climatology.
- **Kenya:** close to climatology, with the highest chances on the western border in week 1 and in the east and north-east in week 2.

If a rerun on the same data and init gives different numbers, find out why before reporting new ones.

## Known limitations
- **Few seasons:** Cumulus overlaps ECMWF for only 6 seasons, about 100 onsets a week per country. One unusual season can change the ranking.
- **Member counts:** live ECMWF has 101 members, but its weights come from the 10-member reforecast, whose shares move in steps of 0.1. Cumulus has 30 members in both.
- **Cells already past onset:** members already past onset count as 0, so the blend gives its lowest probability there. Those cells aren't masked on the maps. The target is undefined once the observed onset has happened, so the blend answers "if the rains haven't started yet".
- **Analog climatology:** with few analog seasons still undecided late in a season, it can jump between weeks (Kenya: 43% then 6%). Use it as context, not as a forecast.
- **Cumulus checkpoint:** the match between `gratis-czajkowski` and the live run is inferred from their identical outputs and member counts. Neither folder records its checkpoint; ask the Cumulus team to confirm.
- **Border cells:** at 1.5°, Kenya's and Uganda's border cells overlap and include parts of neighbours.

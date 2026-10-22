# Runbook: OND 2026 onset forecast for East Africa from ensemble members (mme-fit / mme-blend)

This file tells an agent how to rerun the analysis end to end. It fits one logistic regression per window (7 and 14 days from the forecast date) that turns each model's **ensemble members** into a calibrated chance that the rainy season has started by the end of the window. It checks skill by leaving one season out at a time, then blends the live 2026 forecasts. Places where CHIRPS already shows the onset before the forecast date are set to 1. Results are reported for the whole box and for Kenya and Uganda separately.

Every script lives in `scripts/` next to this file. The previous ensemble-mean version of this analysis is in `RUNBOOK_ensmean.md`, `run_demo_ensmean.sh` and `report_ensmean.py`.

Never fill missing observations: the skills leave a missing sample out, and so does every script here.

## What it computes
- **Event:** the ICPAC rainy-season onset (`icpac-onset`: a 3-day total ≥ 20 mm with no 7-day dry spell, under 1 mm a day, in the next 21 days).
- **Target:** for an init and a window of 7 or 14 days, does the season's **first onset on or after 15 September** come within the window from the init, i.e. have the rains started by day 7 / 14?
  - 1 if yes;
  - 0 if not yet;
  - NaN if the onset had already happened before the init (dropped from fitting and scoring).

  Built with `indicator --rule icpac-onset --first-in 7d` and `--first-in 14d` on each season of CHIRPS, 15 Sep – 31 Jan, stacked along a `window` dim.
- **Predictors:** for each model, the **share of its ensemble members** whose first onset since 15 Sep (by valid date) comes within the window. Built with `indicator --rule icpac-onset --first-in 7d` / `14d` `--season-start 09-15 --probability --time-dim step` on the members, read at the init (lead 0, or the Cumulus lag), with dims `(time, step=0, window, latitude, longitude)`.
- **Windows:** 7 and 14 days only. The onset rule looks 27 days past the trigger, so a 46-day forecast can decide it only through about day 19.
- **Model:** `mme-fit --model logistic --fit-along window`. One unpenalized logistic regression per window, pooled over cells and seasons.
- **Already started:** where CHIRPS (final and preliminary, 15 Sep to the day before the init) confirms an onset, the 2026 blends and climatologies are set to 1. An onset is confirmed only 21 days after its trigger, so onsets that began in the last three weeks aren't marked.
- **Region:** the 1.5° cells centred 6°S – 7.5°N, 28.5°E – 48°E (10 × 14). Northern Ethiopia, whose main rains are June – September, is left out. Kenya and Uganda are each scored on every cell that touches the country (`clip-region --region`), so border cells count for both: 36 cells for Kenya, 17 for Uganda.

## Quick run (from the local archive)
```bash
export SKILLS=/path/to/weather-skills/skills          # branch gates-demo
export PLOT=/path/to/weather-skills-plotting/skills/plot/scripts/plot.py
export HERE=/path/to/cirrus-onset-demo/scripts         # the demo kit (gs://sheerwater-datalake/gates-demo/scripts)
export ARCHIVE=/path/to/archive                       # a copy of gs://sheerwater-datalake/gates-demo/archive
export RUN=/path/to/scratch/run                       # working files, about 370 MB; rebuilt every run
export OUT=/path/to/results                           # report.md and the maps
export INIT=2026-10-15                                # the live init to blend
bash $HERE/run_demo.sh
```
The run only needs these parts of the archive (3.6 GB):
- `chirps_daily_1p5.zarr`;
- `ecmwf_reforecast.zarr`;
- `cumulus_hindcast.zarr`;
- `gefs_reforecast.zarr` and `gefs_operational.zarr` (it adds 2020 – 2021; without it GEFS uses the reforecast alone);
- `live/chirps_2026.zarr`;
- `live/ecmwf/ecmwf_$INIT.zarr`, `live/cumulus/cumulus_$INIT.zarr`, `live/gefs/gefs_$INIT.zarr`.

Timings on 21 Oct 2026 (Apple silicon, local disk):

| Stage | Script(s) | Time |
|---|---|---|
| Observed onsets | `members_events.py` | 21 s |
| Member predictors, hindcast and live | `members_predictors.py ... 2022 cumulus gefs`, `members_live.py ... ecmwf cumulus gefs` | 142 s |
| Cross-validation | `clim.py`, `cv.py ... cumulus-no-gefs`, `cv.py ... gefs` | 25 s |
| Final fits, 2026 blends, already-started cells | `fit_blend.py ... ecmwf cumulus ecmwf+cumulus gefs ecmwf+gefs`, `started.py` | 4 s |
| Maps and report | `clip-region`, `plot` (five maps), `report.py` | 27 s |
| **Total** | | **219 s** |

The member predictors run `indicator` once per window, so they take about twice as long as with one window.

Every script calls the skills in-process through `scripts/skillrun.py`: the same code as `uv run <script>`, without a new process per call. Run the scripts with `uv run --project "$SKILLS/.." python -I` (define `py() { uv run --project "$SKILLS/.." python -I "$@"; }`; a `$PY` string is not split into words by zsh). `-I` keeps Python from importing from the current directory, because the scripts read downloaded data.

## The archive
`gs://sheerwater-datalake/gates-demo/archive/` (private; the demo service account can read it), about 4.4 GB, all cut to Ethiopia + Kenya + Uganda (15.75°N – 6.75°S, 27.75°E – 48.75°E) and September – December inits. Copy all of it with:
```bash
gcloud storage rsync -r gs://sheerwater-datalake/gates-demo/archive $ARCHIVE
```

### Giving someone read access
Run these yourself, as a bucket admin. The access is read-only and limited to `gates-demo/`: the
condition allows objects under that prefix, and listings that ask for it. The condition goes in a
file, because `--condition` splits its value at the comma inside the expression.
```bash
cat > gates-demo-condition.json <<'EOF'
{
  "title": "gates-demo-read",
  "description": "Read and list only under gates-demo/",
  "expression": "resource.name.startsWith(\"projects/_/buckets/sheerwater-datalake/objects/gates-demo/\") || (resource.type == \"storage.googleapis.com/Bucket\" && api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"gates-demo/\"))"
}
EOF
```
A person, by their Google account:
```bash
gcloud storage buckets add-iam-policy-binding gs://sheerwater-datalake --member=user:NAME@example.com \
  --role=roles/storage.objectViewer --condition-from-file=gates-demo-condition.json
```
The demo service account and its key, for `GOOGLE_APPLICATION_CREDENTIALS` (keep the key out of every
repo and synced folder; delete it after the demo with `gcloud iam service-accounts keys delete`):
```bash
gcloud iam service-accounts create gates-demo-reader --project=sheerwater --display-name="Gates demo: read gates-demo/"
gcloud storage buckets add-iam-policy-binding gs://sheerwater-datalake \
  --member=serviceAccount:gates-demo-reader@sheerwater.iam.gserviceaccount.com \
  --role=roles/storage.objectViewer --condition-from-file=gates-demo-condition.json
gcloud iam service-accounts keys create ~/gates-demo-key.json --iam-account=gates-demo-reader@sheerwater.iam.gserviceaccount.com
```
Check: with the new account, `gcloud storage ls gs://sheerwater-datalake/gates-demo/archive/` lists
the stores and `gcloud storage ls gs://sheerwater-datalake/` is refused. Remove access with
`remove-iam-policy-binding` and the same member, role and condition. A personal Gmail address fails
if the organization restricts IAM members to its own domain.

| Store | Contents | Size |
|---|---|---|
| `ecmwf_reforecast.zarr` | WeatherBench2 IFS extended-range reforecast, **10 members**: 244 Sep – Dec issuances 2016 – 2022 (Mon/Thu) × 20 hindcast years each (1996 – 2021) × 46 daily leads, 1.5°. `total_precipitation_24hr` in metres per day (× 1000 for mm). The 2-D `time` coord holds the real init dates. | 830 MB |
| `ecmwf_realtime.zarr` | The same issuances' real-time forecasts, 50 members. | 203 MB |
| `cumulus_hindcast.zarr` | Cumulus (Aurora S2S) hindcast run `gratis-czajkowski`, the one matching the live run: 558 Sep – Dec inits 2016 – 2024 (every 2 days) × **30 members** × 46 daily steps, 1° IMERG-trained `precip` in mm day-1. | 1.3 GB |
| `gefs_reforecast.zarr` | Sheerwater GEFSv12 reforecast: 1073 Wednesday inits 1999 – 2019 × **11 members** × 35 daily leads, 1°. Open with `decode_times=False` (`init_time` in days since 1960-01-01). | 203 MB |
| `gefs_operational.zarr` | Operational GEFS, every Sep – Dec init 2020 – 2025 (702, from 1 Oct 2020), 31 members, 35 daily steps, daily means on the 1.5° cells (`precipitation_surface`, mm day-1). Built on a cloud VM with skills only (`scripts/gefs_job/`). | 1.3 GB |
| `chirps_daily.zarr` | CHIRPS final, Sep – Jan of each season 1981 – 2026, daily, 0.05°, mm day-1. | 1.6 GB |
| `chirps_daily_1p5.zarr` | The same, coarsened conservatively onto the 1.5° cells of `live/grid_1p5.zarr` (15 × 14). | 3.5 MB |
| `tahmo/` | TAHMO daily station observations, Ethiopia, Kenya and Uganda, one file per year 2015 – 2026, plus `stations.csv`. | 10 MB |
| `live/ecmwf/` | Live ECMWF 46-day ensemble, 101 members, 1.5°, one store per init since 1 Sep 2026. | 86 MB |
| `live/cumulus/` | Live Cumulus (`utmost-plane`), 30 members, 1°, inits 3 – 21 Oct 2026. | 47 MB |
| `live/gefs/` | Live GEFS, 31 members, daily means at 1.5°, every init since 1 Sep 2026. | 64 MB |
| `live/grid_1p5.zarr` | The 1.5° target grid. | |
| `live/chirps_2026.zarr` | CHIRPS final and preliminary, 15 Sep – 15 Oct 2026, on the 1.5° cells (`chirps-fetch`, then `coarsen --reference-grid live/grid_1p5.zarr --method conservative`). Refresh it before a later init. | 0.1 MB |

GEFS enters the analysis for the 7-day window only (35-day forecasts), scored separately from Cumulus because the seasons they share with ECMWF barely overlap (2016, 2020 and 2021).

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
4. Stack each season's 7- and 14-day results along a `window` dim (own code: `concat` refuses inputs whose `aggregation_period` differs, which these do by design), then `concat` the seasons into `obs_event.zarr`.

### 2. Member predictors
```bash
py $HERE/members_predictors.py $ARCHIVE $RUN 2022 cumulus gefs
py $HERE/members_live.py $ARCHIVE $RUN $INIT ecmwf cumulus gefs
```
- **ECMWF:** the 2022 model cycle's Thursday issuances (hindcast years 2002 – 2021), reshaped from WeatherBench2's issuance × hindcast-year layout to one init axis (own code, since no skill reads that layout). Then `clip-region` and `indicator`. This gives `ecmwf_hind.zarr`, 360 inits, with dims `(time, step=0, window=7/14, latitude, longitude)`.
- **Cumulus:** for each ECMWF init, the latest Cumulus init on or before it, at most 1 day earlier. It is read at lead day lag, so the windows start on the same valid date, then put on the 1.5° cells with `coarsen --method conservative`. This gives `cumulus_hind.zarr`, 108 inits (2016 – 2021).
- **GEFS:** paired the same way (lag 0 – 1 day). The reforecast runs on Wednesdays, so it lines up with the ECMWF hindcast dates (which keep the 2022 calendar dates in every year) in only 5 seasons: 2004, 2005, 2010, 2011, 2016. It is reshaped from its IRIDL layout (own code), run through `indicator`, and coarsened from 1° to the 1.5° cells. `gefs_operational.zarr` (daily inits, already at 1.5°) adds every 2020 – 2021 init at lag 0, for 7 seasons in all, with `select` and `clip-region`. GEFS runs 35 days, which decides onset only within 7 days, so its 14-day share is set to NaN. This gives `gefs_hind.zarr`.
- **Live:** the same for `$INIT`, giving `live/ecmwf_wk.zarr`, `live/cumulus_wk.zarr` and `live/gefs_wk.zarr`.

Cumulus's 1° grid doesn't fully cover the southern row and the eastern column of 1.5° cells, so those are NaN for Cumulus (about 16% of cells).

### 3. Climatology and cross-validation
```bash
EC_INIT=$INIT py $HERE/clim.py $RUN
py $HERE/cv.py $RUN cumulus-no-gefs > $RUN/cv.txt
py $HERE/cv.py $RUN gefs > $RUN/cv_gefs.txt
```
- **`clim.py`** writes, for the same inits and windows, the chance of the event from that day of the season:
  - `clim_hind.zarr`: every 1981 – 2025 season except the init's own;
  - `clim_2026_analog.zarr`: the 2026 El Niño analog years 1982 1994 1997 2006 2015 2019 2023;
  - `clim_2026_all.zarr`: all years.
- **`cv.py`:** for each of the 6 seasons ECMWF and Cumulus share (2016 – 2021), it fits on the other 5 with `mme-fit`, blends the held-out season with `mme-blend`, and scores it. The sets are `ecmwf`, `cumulus`, `ecmwf+cumulus` and `clim`. Every set is scored on the same cell-weeks: those where the observation and every input are present. Rows go to `cv_predictions_cumulus-no-gefs.csv`, with each sample's cell.
- **`cv.py ... gefs`:** the same over the seasons ECMWF and GEFS share, for `ecmwf`, `gefs`, `ecmwf+gefs` and `clim`, 7 days only (`cv_predictions_gefs.csv`).

Other modes: `cv.py $RUN no-gefs` scores ECMWF alone and ECMWF + climatology over all 20 ECMWF seasons. The modes `cumulus` and the default include GEFS.

### 4. Final fits, 2026 blends and already-started cells
```bash
py $HERE/fit_blend.py $RUN ecmwf cumulus ecmwf+cumulus gefs ecmwf+gefs
py $HERE/started.py $ARCHIVE $RUN $INIT p2026_ecmwf p2026_cumulus p2026_ecmwf+cumulus p2026_gefs p2026_ecmwf+gefs \
  clim_2026_analog clim_2026_all
```
- `fit_blend.py` fits on all seasons (`--fit-along window`) and blends the live forecasts, writing `w_<set>.zarr` and `p2026_<set>.zarr`.
- `started.py` runs `indicator --rule icpac-onset --cumulative` on `live/chirps_2026.zarr` and clips it to the box (`started.zarr`). It sets each named 2026 file to 1 where the onset is confirmed by the day before the init (only in windows the blend forecasts, so GEFS's 14-day window stays empty): 20 cells for the 15 October init, 9 of them in Uganda and none in Kenya.

### 5. Maps and report
`run_demo.sh` plots `p2026_<set>.zarr` for all cells, one panel per window (`"meta": {"facet": "window"}`), titled with the window's last day ("By 21 Oct", "By 28 Oct") through the `panel-title-1` / `panel-title-2` annotations. It clips the cells (`cells_Kenya.zarr`, `cells_Uganda.zarr`) and the ECMWF + Cumulus blend to each country, plots those, and runs:
```bash
py $HERE/report.py $OUT/report.md $RUN
```
The report has the skill table, weights and 2026 outlook for all cells, Kenya and Uganda.

## Results (run of 21 October 2026, init 15 October 2026)
**Cross-validated Brier skill score against climatology**, by day 7 / day 14, 2016 – 2021 (6 seasons). Above 0 beats climatology.

| Predictors | All cells (415 / 693 onsets) | Kenya (118 / 217) | Uganda (93 / 144) |
|---|---|---|---|
| ECMWF | +0.04 / +0.00 | +0.11 / +0.07 | +0.10 / +0.04 |
| Cumulus | +0.22 / +0.20 | +0.31 / +0.27 | +0.29 / +0.35 |
| ECMWF + Cumulus | **+0.24 / +0.23** | **+0.33 / +0.29** | **+0.31 / +0.37** |

**GEFS**, within 7 days only (the report and dashboard list these rows in the same tables as the other models), on the 7 seasons GEFS shares with ECMWF (2004, 2005, 2010, 2011, 2016 from the reforecast; 2020, 2021 operational):

| Predictors | All cells (428 onsets) | Kenya (111) | Uganda (87) |
|---|---|---|---|
| ECMWF | +0.00 | +0.01 | −0.20 |
| GEFS | +0.06 | +0.18 | −0.06 |
| ECMWF + GEFS | **+0.10** | **+0.18** | −0.04 |

Weights for 7 days: GEFS alone −3.73 + 7.48 × GEFS; ECMWF + GEFS −4.08 + 2.96 × ECMWF + 4.57 × GEFS. For 15 October 2026 the ECMWF + GEFS blend gives 26% (all cells), 6% (Kenya) and 68% (Uganda) by 21 October.

**Weights** (log-odds per unit of members' onset share, fitted on all seasons):

| Blend | Window | Intercept | Coefficients |
|---|---|---|---|
| ECMWF | 7 / 14 days | −3.50 / −3.14 | ECMWF 4.79 / 4.61 |
| Cumulus | 7 / 14 days | −3.81 / −3.46 | Cumulus 7.12 / 7.18 |
| ECMWF + Cumulus | 7 / 14 days | −3.97 / −3.68 | ECMWF 1.70 / 1.90, Cumulus 5.75 / 5.61 |

**2026 outlook**: the area-mean chance that the rains have started by 21 Oct / 28 Oct. Cells where CHIRPS already shows the onset count as 1.

| Forecast | All cells (20 started) | Kenya (0 started) | Uganda (9 started) |
|---|---|---|---|
| ECMWF + Cumulus blend | 26% / 35% | 8% / 19% | 76% / 80% |
| Cumulus blend | 26% / 34% | 8% / 19% | 77% / 80% |
| ECMWF blend | 26% / 35% | 8% / 20% | 67% / 72% |
| Climatology, all years | 30% / 37% | 11% / 18% | 65% / 76% |

What the outlook shows:
- **Uganda:** the rains have already started in the north and west (CHIRPS). Around Lake Victoria the blend gives high chances by 21 October, above climatology.
- **Kenya:** none started yet. The chance by 28 October is about 1 in 5, close to climatology. It's highest near Lake Victoria and in the east and north-east.

If a rerun on the same data and init gives different numbers, find out why before reporting new ones.

## Known limitations
- **Few seasons:** Cumulus overlaps ECMWF for only 6 seasons, about 100 onsets a week per country. One unusual season can change the ranking.
- **Member counts:** live ECMWF has 101 members, but its weights come from the 10-member reforecast, whose shares move in steps of 0.1. Cumulus has 30 members in both.
- **Recent onsets:** CHIRPS confirms an onset only 21 days after its first rains, so places whose rains began in the three weeks before the init aren't set to 1. There the blend answers "if the rains haven't started yet".
- **Analog climatology:** only 7 seasons, and it counts only those where the rains had not started by the same day, so it rests on very few seasons (Kenya: 43% by 21 Oct, against 11% over all years). Use it as context, not as a forecast.
- **Cumulus checkpoint:** the match between `gratis-czajkowski` and the live run is inferred from their identical outputs and member counts. Neither folder records its checkpoint; ask the Cumulus team to confirm.
- **Border cells:** at 1.5°, Kenya's and Uganda's border cells overlap and include parts of neighbours.

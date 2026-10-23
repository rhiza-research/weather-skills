# Runbook: Cirrus onset blend from the tiled hindcast stores (skills only)

This tests the Cirrus method, a logistic regression (`mme-fit`) that turns each model's ensemble members into a calibrated chance that the East African OND rains have started within 7 or 14 days. It uses only weather-skills: the hindcasts come from Sheerwater's tiled stores through `hindcast-fetch`, the observations from CHIRPS, and the skill score from `verify --probability`. Shell only loops over seasons and folds, and picks the init dates the three hindcasts share.

Never fill missing observations or forecasts: the skills leave a missing sample out.

## Run
```bash
gcloud auth application-default login   # Cumulus is in the private gs://sheerwater-datalake
WORK=/path/to/scratch bash demos/cirrus-hindcast-blend/scripts/run.sh
```
About 11 minutes and 1 GB of working files (23 Oct 2026, Apple silicon). Without `WORK`, files go to `scripts/work/`. Finished steps are skipped on a rerun; delete `WORK` to start over.

Credentials: ECMWF and GEFS hindcasts are public. Cumulus needs Google Cloud access to `gs://sheerwater-datalake/hindcasts`: Application Default Credentials, `GOOGLE_APPLICATION_CREDENTIALS`, or `NEURAL_GCM_SERVICE_CREDENTIALS` (the key JSON itself). CHIRPS is public.

## What it computes
- **Event:** the season's first ICPAC onset (`icpac-onset`: a 3-day total ≥ 20 mm with no 7-day dry spell under 1 mm a day in the next 21 days) on or after 15 Sep falls within 7 / 14 days of the init. 1 if it does, 0 if not yet, NaN if the onset already happened. `indicator --first-in 7d|14d` on each season of CHIRPS (15 Sep – 31 Jan), on the 1.5° cells (`coarsen --method conservative`).
- **Predictors:** for each model, the share of its members whose first onset since 15 Sep falls within the window, read at the init: `indicator --rule icpac-onset --first-in 7d|14d --season-start 09-15 --probability --time-dim step`, then `select --dim step --index 0`. Cumulus and GEFS (1°) are then put on the 1.5° cells with `coarsen --method conservative`.
- **Hindcasts:** `hindcast-fetch` reads `ecmwf_ifs_precip.zarr` (10 perturbed members, 1.5°, newest run per date), `gefs_precip.zarr` (11 members, 1°, Wednesdays) and `cumulus_precip.zarr` (run `gratis-czajkowski`, 30 members, 1°, negatives set to 0).
- **Inits:** 15 Sep – 31 Dec of 2016, 2017 and 2018 (the only autumns all three share: GEFS ends in August 2019), on dates all three ran: 7, 8 and 6 inits. `mme-fit` pairs forecasts on identical init dates.
- **Windows:** 7 days for all three; 14 days for ECMWF and Cumulus (GEFS runs 35 days, which decides onset only within 7).
- **Model:** `mme-fit --model logistic`, one unpenalized fit per window, pooled over cells and inits.
- **Skill:** leave one season out (fit on two, `mme-blend` the third), the three held-out seasons scored together with `verify --metric brier --probability`. The BSS reference is the observed base rate. Scored twice: on each set's own samples, then on the samples every model shares (`mme-blend` with the fold's weights and weight 0 on the other models), which is the fair comparison.
- **Grid:** the 140 ECMWF 1.5° cells 6°S – 7.5°N, 28.5°E – 48°E.

## Results (23 Oct 2026)
Every set has the same 972 cell-inits, so the own-sample and common-sample scores agree. Brier skill score (7 days: base rate 5%; 14 days: 12%):

| Predictors | 7 days | 14 days |
|---|---|---|
| ECMWF | +0.185 | +0.212 |
| Cumulus | **+0.420** | +0.370 |
| GEFS | +0.098 | — |
| ECMWF + Cumulus | +0.417 | **+0.375** |
| ECMWF + Cumulus + GEFS | +0.394 | — |

Weights fitted on all seasons, 7 days: ECMWF + Cumulus −5.12 + 1.20 × ECMWF + 8.26 × Cumulus; with GEFS −5.07 + 1.73 × ECMWF + 9.34 × Cumulus − 2.24 × GEFS. Cumulus carries the skill; blending adds little or nothing. Three seasons and 21 inits only, so differences of a few hundredths are within the noise. Clipping Cumulus's small negative values at 0 left its predictors unchanged.

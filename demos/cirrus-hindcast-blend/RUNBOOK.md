# Runbook: Cirrus onset blend from the tiled hindcast stores (skills only)

This tests the Cirrus method, a logistic regression (`mme-fit`) that turns each model's ensemble members into a calibrated chance that the East African OND rains have started within 7 days. It uses only weather-skills: hindcasts from Sheerwater's tiled stores through `hindcast-fetch`, observations from CHIRPS, and the skill score from `verify --probability`. Shell only loops over seasons and folds, and one line of Python finds the init dates all three hindcasts share.

Never fill missing observations or forecasts: the skills leave a missing sample out.

## Run
```bash
WORK=/path/to/scratch CACHE=/path/to/cache bash demos/cirrus-hindcast-blend/scripts/run.sh
```
About 5 minutes the first time and 3 minutes after that, with about 1 GB of working files. `WORK` (default `scripts/work/`) is emptied at the start of every run.

`CACHE` (default `scripts/cache/`) keeps the three fetched hindcasts between runs, about 0.5 GB. Each store is named after its exact `hindcast-fetch` arguments (dataset, dates, bbox, members), so changing any of them fetches a new one, and a fetch is renamed into the cache only once it succeeds. The cache never notices when a source store is rewritten (as the Cumulus store was when its negatives were clipped): delete `CACHE` then.

Credentials: ECMWF, GEFS and CHIRPS are public. Cumulus is in the private `gs://sheerwater-datalake/hindcasts/cumulus_precip.zarr` and needs Google Cloud access to it: Application Default Credentials, `GOOGLE_APPLICATION_CREDENTIALS` (a service-account key file) or `NEURAL_GCM_SERVICE_CREDENTIALS` (the key JSON itself). The Gates demo account `gates-demo-reader` reads only `gates-demo/` unless it is also granted `hindcasts/cumulus_`; without that the Cumulus fetch exits 1.

## What it computes
- **Event:** the season's first ICPAC onset (`icpac-onset`: a 3-day total ≥ 20 mm with no 7-day dry spell under 1 mm a day in the next 21 days) on or after 15 Sep falls within 7 days of the init: 1 if it does, 0 if not yet, NaN if the onset already happened. `indicator --first-in 7d --season-start 09-15` on CHIRPS 15 Sep 2016 – 31 Jan 2019, put on the 1.5° cells with `coarsen --method conservative`.
- **Predictors:** each model's share of members whose first onset since 15 Sep falls within 7 days of the init: `indicator --rule icpac-onset --first-in 7d --season-start 09-15 --probability --time-dim step`, then `select --dim step --index 0`. Cumulus and GEFS (1°) are then put on the 1.5° cells with `coarsen --method conservative`.
- **Hindcasts:** `hindcast-fetch` reads `ecmwf_ifs_precip.zarr` (10 perturbed members, 1.5°, newest run per date), `gefs_precip.zarr` (11 members, 1°, Wednesdays) and `cumulus_precip.zarr` (run `gratis-czajkowski`, 30 members, 1°, negatives set to 0).
- **Inits:** 15 Sep – 31 Dec of 2016, 2017 and 2018 (the only autumns all three share: GEFS ends in August 2019), on dates all three ran: 7, 8 and 6 inits. `mme-fit` pairs forecasts on identical init dates.
- **Model:** `mme-fit --model logistic`, unpenalized, pooled over cells and inits. Sets: ECMWF, Cumulus, GEFS, and all three together.
- **Skill:** leave one season out (fit on two, `mme-blend` the third), the three held-out seasons scored together with `verify --metric brier --probability`. The BSS reference is the observed base rate. Every set has the same 972 cell-inits.
- **Grid:** the 140 ECMWF 1.5° cells 6°S – 7.5°N, 28.5°E – 48°E.

## Results (23 Oct 2026)
Brier skill score within 7 days (base rate 5%):

| Predictors | Brier | BSS |
|---|---|---|
| ECMWF | 0.0413 | +0.185 |
| Cumulus | 0.0294 | **+0.420** |
| GEFS | 0.0457 | +0.098 |
| ECMWF + Cumulus + GEFS | 0.0307 | +0.394 |

Cumulus carries the skill; blending in ECMWF and GEFS does not improve on it. Three seasons and 21 inits only, so differences of a few hundredths are within the noise.

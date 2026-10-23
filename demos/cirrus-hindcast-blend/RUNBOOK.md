# Runbook: onset blend from the tiled hindcast stores, any African country (skills only)

This tests the Cirrus / University of Chicago blend, a logistic regression (`mme-fit`) that turns each model's ensemble members into a calibrated chance that the rainy season has started within 7 (or 14) days, for any country. It uses only weather-skills: hindcasts from Sheerwater's tiled stores through `hindcast-fetch`, observations from CHIRPS, the country from `resolve-region` and `clip-region`, and the skill score from `verify --probability`. Shell loops over seasons and folds; short Python lines only do date and box arithmetic and find the init dates the chosen models share.

Never fill missing observations or forecasts: the skills leave a missing sample out.

## Run
```bash
COUNTRY=Kenya MODELS="ec cu" bash demos/cirrus-hindcast-blend/scripts/run.sh
```
Settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `COUNTRY` | `Kenya` | Country name or ISO3 code (`Ghana`, `COD`). |
| `MODELS` | `ec cu` | Any of `ec` (ECMWF), `cu` (Cumulus), `ge` (GEFS). Each is scored alone, and all together. |
| `SEASON_START` | `09-15` | `MM-DD` the onset count restarts each year. |
| `INIT_DAYS` | `107` | Inits from `SEASON_START` to this many days later (107: to 31 Dec). |
| `RULE` | `icpac-onset` | `indicator` onset rule: `icpac-onset`, `chc-onset`, or a clause string. |
| `WINDOW` | `7` | Days from the init within which the onset must fall: 7 or 14 (GEFS: 7 only). |
| `FIRST`, `LAST` | all | Limit the seasons (the years they start in). |
| `WORK` | `scripts/work/` | Working files, emptied at the start of every run. |
| `CACHE` | `scripts/cache/` | Fetched hindcasts kept between runs. |

**Seasons** are the years every chosen model has hindcasts for: ECMWF 1996–2021, GEFS Dec 1998 – Aug 2019, Cumulus 2016–2024.

| `MODELS` | Seasons (Sep start) |
|---|---|
| `ec cu` | 2016–2021 (6) |
| `ec ge` | 1999–2018 (20) |
| `ec cu ge` | 2016–2018 (3) |
| `ec` | 1996–2021 (26) |

**Season start and rule** depend on the region's rainfall regime; the defaults are the East African short rains. Starting points to adapt: East African long rains `03-01`; West Africa and the Sahel `03-01` to `05-01` with `chc-onset`; southern Africa `10-01` (the season crosses New Year, which the script handles).

**Time:** Kenya with `ec cu` (6 seasons) took 12 minutes with an empty cache; Ghana 10. Longer runs (`ec ge`, 20 seasons) scale with the number of seasons. A rerun reads the hindcasts from `CACHE`.

**Credentials:** ECMWF, GEFS and CHIRPS are public. Cumulus is in the private `gs://sheerwater-datalake/hindcasts/cumulus_precip.zarr`: Application Default Credentials, `GOOGLE_APPLICATION_CREDENTIALS` (a service-account key file) or `NEURAL_GCM_SERVICE_CREDENTIALS` (the key JSON). The Gates demo account `gates-demo-reader` can read it.

**Cache:** one store per season, model and box, named after its exact `hindcast-fetch` arguments; a fetch is renamed in only once it succeeds. The cache never notices when a source store is rewritten: delete `CACHE` then.

## What it computes
- **Cells:** the 1.5° ECMWF cells covering the country's box (`resolve-region`). The 1° models are fetched over a box 1.5° wider and put on those cells with `coarsen --method conservative`. Only cells that touch the country (`clip-region --region`) count in the fits and scores.
- **Event:** the season's first onset (`RULE`) on or after `SEASON_START` falls within `WINDOW` days of the init: 1 if it does, 0 if not yet, NaN if the onset already happened. `indicator --first-in` on each season of CHIRPS from `SEASON_START`, on the 1.5° cells.
- **Predictors:** each model's share of members whose first onset since `SEASON_START` falls within the window: `indicator --first-in --season-start --probability --time-dim step`, then `select --dim step --index 0`.
- **Inits:** per season, the dates every chosen model ran (`mme-fit` pairs identical init dates). ECMWF runs almost daily, Cumulus every second day, GEFS on Wednesdays. The ECMWF store has fewer dates in its last years (2020, 2021).
- **Model:** `mme-fit --model logistic`, unpenalized, pooled over cells and inits.
- **Skill:** leave one season out (fit on the others, `mme-blend` the held-out one), all held-out seasons scored together with `verify --metric brier --probability`. The BSS reference is the observed base rate, not a day-of-season climatology.

## Results (23 Oct 2026, `MODELS="ec cu"`, 2016–2021, within 7 days)

| Country, season, rule | Base rate | ECMWF | Cumulus | ECMWF + Cumulus |
|---|---|---|---|---|
| Kenya, from 15 Sep, `icpac-onset` | 7% | +0.139 | +0.316 | **+0.326** |
| Ghana, from 1 Mar, `chc-onset` | 25% | +0.095 | +0.287 | **+0.289** |

Kenya with all three models over 2016–2018 only: ECMWF +0.185, Cumulus +0.420, GEFS +0.098, all three +0.394 (base rate 5%). Cumulus carries most of the skill; over six seasons, adding ECMWF helps a little.

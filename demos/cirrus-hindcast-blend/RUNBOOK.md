# Runbook: rainfall-event blend from the tiled hindcast stores, any African country (skills only)

This tests the Cirrus / University of Chicago blend, a logistic regression (`mme-fit`) that turns each model's ensemble members into a calibrated chance of a rainfall event, for any country: by default the rainy-season onset within 7 days, or any `indicator` rule such as a dry spell. It uses only weather-skills: hindcasts from Sheerwater's tiled stores through `hindcast-fetch`, observations from CHIRPS, the country from `resolve-region` and `clip-region`, and the skill score from `verify --probability`. Shell loops over seasons and folds; short Python lines only do date and box arithmetic and find the init dates the chosen models share.

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
| `EVENT` | `onset` | `onset`: the season's first `RULE` day falls within `WINDOW` days of the init. `rule`: `RULE` itself, true or false at each date. |
| `RULE` | `icpac-onset` | `indicator` rule: `icpac-onset`, `chc-onset`, or a clause string. With `EVENT=rule` the rule carries its own window, e.g. a dry spell starting within 7 days: `"precip consecutive-below 1 7d within 7d"`. |
| `SEASON_START` | `09-15` | `MM-DD` each season starts (and the onset count restarts). |
| `INIT_DAYS` | `107` | Inits from `SEASON_START` to this many days later (107: to 31 Dec). |
| `WINDOW` | `7` | `EVENT=onset`: days from the init within which the onset must fall, 7 or 14 (GEFS: 7 only). |
| `FIRST`, `LAST` | all | Limit the seasons (the years they start in). |
| `WORK` | `scripts/work/` | Working files, emptied at the start of every run. |
| `CACHE` | `scripts/cache/` | Fetched hindcasts kept between runs. |

**Seasons** are the years every chosen model has hindcasts in: ECMWF 1996–2021, GEFS Dec 1998 – Aug 2019, Cumulus 2016–2024. A season where the models share no init date is skipped (ECMWF + GEFS in 1998). A season that the stores only partly cover is kept with fewer inits: a season starting 15 Oct 2015 has Cumulus inits only from January 2016.

| `MODELS` | Seasons (15 Sep start) |
|---|---|
| `ec cu` | 2016–2021 (6) |
| `ec ge` | 1999–2018 (20) |
| `ec cu ge` | 2016–2018 (3) |
| `ec` | 1996–2021 (26) |

**Season start and rule** depend on the region's rainfall regime; the defaults are the East African short rains. Starting points to adapt: East African long rains `03-01`; West Africa and the Sahel `03-01` to `05-01` with `chc-onset`; southern Africa `10-15` with `chc-onset` (the season crosses New Year).

**Time** with an empty cache: Kenya `ec cu` (6 seasons) 12 min, Ghana 10, Zambia (7 seasons, a larger country) 14, Kenya `ec ge` (20 seasons) 27. A rerun reads the hindcasts from `CACHE`: Kenya dry spells with a warm cache took 5 min.

**Credentials:** ECMWF, GEFS and CHIRPS are public. Cumulus is in the private `gs://sheerwater-datalake/hindcasts/cumulus_precip.zarr`: Application Default Credentials, `GOOGLE_APPLICATION_CREDENTIALS` (a service-account key file) or `NEURAL_GCM_SERVICE_CREDENTIALS` (the key JSON). The Gates demo account `gates-demo-reader` can read it.

**Cache:** one store per season, model and box, named after its exact `hindcast-fetch` arguments; a fetch is renamed in only once it succeeds. The cache never notices when a source store is rewritten: delete `CACHE` then.

## What it computes
- **Cells:** the 1.5° ECMWF cells covering the country's box (`resolve-region`). The 1° models are fetched over a box 1.5° wider and put on those cells with `coarsen --method conservative`. Only cells that touch the country (`clip-region --region`) count in the fits and scores.
- **Event:** `EVENT=onset`: the season's first `RULE` day on or after `SEASON_START` falls within `WINDOW` days of the init: 1 if it does, 0 if not yet, NaN if it already happened (`indicator --first-in`). `EVENT=rule`: `RULE` at the init date, 1 or 0. Both on CHIRPS from `SEASON_START`, on the 1.5° cells.
- **Predictors:** each model's share of members for which the event happens, read at the init: `indicator --probability --time-dim step` (with `--first-in --season-start` for onset), then `select --dim step --index 0`.
- **Inits:** per season, the dates every chosen model ran (`mme-fit` pairs identical init dates). ECMWF runs almost daily, Cumulus every second day, GEFS on Wednesdays. The ECMWF store has fewer dates in its last years (2020, 2021).
- **Model:** `mme-fit --model logistic`, unpenalized, pooled over cells and inits.
- **Skill:** leave one season out (fit on the others, `mme-blend` the held-out one), all held-out seasons scored together with `verify --metric brier --probability`. The BSS reference is the observed base rate, not a day-of-season climatology.

## Results (23 Oct 2026)
Brier skill score of the held-out seasons; the best in each row in bold.

| Country, event | Models, seasons | Base rate | ECMWF | Cumulus | GEFS | Blend |
|---|---|---|---|---|---|---|
| Kenya, onset within 7 d from 15 Sep, `icpac-onset` | `ec cu`, 2016–2021 | 7% | +0.139 | +0.316 | | **+0.326** |
| Ghana, onset within 7 d from 1 Mar, `chc-onset` | `ec cu`, 2016–2021 | 25% | +0.095 | +0.287 | | **+0.289** |
| Zambia, onset within 7 d from 15 Oct, `chc-onset` | `ec cu`, 2015–2021 | 32% | +0.253 | +0.320 | | **+0.321** |
| Kenya, onset within 7 d from 15 Sep, `icpac-onset` | `ec ge`, 1999–2018 | 6% | +0.122 | | +0.093 | **+0.141** |
| Kenya, onset within 7 d from 15 Sep, `icpac-onset` | `ec cu ge`, 2016–2018 | 5% | +0.185 | **+0.420** | +0.098 | +0.394 |
| Kenya, 7-day dry spell starting within 7 d, from 15 Sep | `ec cu`, 2016–2021 | 38% | +0.331 | +0.396 | | **+0.427** |

- Cumulus carries most of the onset skill; with six or more seasons the blend matches or slightly beats it. Over 20 seasons ECMWF + GEFS blend better than either alone, with near-equal weights.
- Dry spells (`EVENT=rule RULE="precip consecutive-below 1 7d within 7d"`) are where blending helps most. Their base rate includes the dry weeks before the rains start, so part of that skill is forecasting dry weather in the dry season; for dry spells after onset only, the event also has to require the onset.
- Three seasons (all three models) is too few to rank models: differences of a few hundredths are noise there.

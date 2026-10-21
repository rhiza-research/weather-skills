# Runbook: OND 2026 onset forecast for Kenya with mme-fit / mme-blend

This file tells an agent how to rerun the analysis end to end. It fits logistic regression blend weights on 20 OND seasons of ECMWF and GEFS hindcasts against CHIRPS onset dates, checks skill by leaving one season out at a time, and blends the live 2026 forecasts into onset-week probabilities.

Every script lives in `scripts/` next to this file.
- **First time:** follow **Get everything downloaded** (about 25 minutes, mostly unattended).
- **After that:** run the whole analysis with `scripts/run_all.sh`, which takes **1–2 minutes** (see **Quick run**). Only the live 2026 forecasts are fetched over the network.

Never fill missing observations: the skills leave a missing sample out, and so does every script here.

## What it computes
- **Event:** ICPAC onset (`icpac-onset`: 3-day total ≥ 20 mm and no 7-day dry spell (< 1 mm a day) in the next 21 days), counted from 1 October of each season.
- **Target:** for lead week *k* after an init, does the first onset since 1 October fall in that week? It is 1 if yes, 0 if not yet, and NaN if onset already happened earlier. NaN samples are dropped.
- **Predictors:** each model's ensemble-mean weekly rain (mm/day) for that lead week, and optionally a climatological probability.
- **Model:** `mme-fit --model logistic --fit-along step`, a separate logistic regression for each lead week, pooled over cells and seasons. It is unpenalized (`alpha` 0).

## Get everything downloaded
Do this once on a new machine. The first full download takes about 2 hours, mostly unattended (all downloads run at once; TAHMO is the longest), and leaves about 7.4 GB in `$ARCHIVE`. Nothing already downloaded is fetched again: every piece is written under a temporary name and renamed only when complete, so an existing store, season, year or live forecast is always skipped, and an interrupted download resumes where it stopped. To refresh something, delete it and rerun.

**1. Install the tools.**
- `git`, and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`). `uv` installs Python 3.12 itself.
- The Google Cloud CLI (`gcloud`, https://cloud.google.com/sdk/docs/install), for credentials only.
- macOS is assumed: `3_forecasts_2026.sh` uses BSD `date -j` (on Linux use `date -d`), and the scripts avoid bash-4 features because macOS ships bash 3.2.

**2. Get the code.**
```bash
git clone -b gates-demo https://github.com/rhiza-research/weather-skills
git clone https://github.com/rhiza-research/weather-skills-plotting      # for the plot skill
(cd weather-skills && uv sync --group dev)                                # one environment with every skill's dependencies
```
The `gates-demo` branch has `mme-fit`, `mme-blend` and `indicator --first-in`. Every skill on it pins `weather-skills-core>=0.1.1` from PyPI, so `uv` doesn't fetch the core git branch on every call; a cached call takes about 1.4 s instead of about 2.6 s. The one exception is `label-group`, which still needs core `dev`.

Also copy this `ond2026-onset/` folder (the runbook and its `scripts/`) onto the machine. It is not in any repository.

**3. Set up the credentials.**

| Data | Credential |
|---|---|
| GEFS reforecast (`gs://sheerwater-datalake`) | A Google account that can read it: `gcloud auth application-default login` |
| Cumulus hindcast (Azure `italynorthdata/data`) | `export AZURE_STORAGE_SAS_TOKEN=...`: a read SAS (`sp=rl`) for that container, the same one the `cumulus-fetch` skill uses. SAS tokens expire; ask the data owner for a new one if access is refused. |
| TAHMO stations | `export TAHMO_API_USERNAME=...` and `export TAHMO_API_PASSWORD=...` |
| ECMWF hindcasts (WeatherBench2), CHIRPS, live forecasts (dynamical.org) | Public; nothing needed |

Never print or commit these values.

**4. Set the paths.** Set these in every new shell before any step:
```bash
export SKILLS=$PWD/weather-skills/skills
export PLOT=$PWD/weather-skills-plotting/skills/plot/scripts/plot.py
export HERE=/path/to/ond2026-onset/scripts        # this folder's scripts/
export ARCHIVE=/path/to/archive                   # downloads, about 7.4 GB; keep it
export WORK=/path/to/scratch/ond                  # per-run working files, under 20 MB; safe to delete
export OUT=/path/to/ond2026-onset                 # maps and score tables
mkdir -p "$ARCHIVE" "$WORK" "$OUT"
py() { uv run --project "$SKILLS/.." python -I "$@"; }   # Python in the skills environment
```
- **`py` is a function:** a `$PY` string would not be split into words by zsh.
- **`-I`:** isolates Python from the current directory, because the scripts read downloaded data.
- **In-process calls:** `events.py` and `cv.py` call the skills in-process through `scripts/skillrun.py`. That is the same code as `uv run <script>` without a new process per call, which turns 15 minutes of cross-validation into 23 seconds.

**5. Download and check everything.**
```bash
bash $HERE/download_all.sh
```
It runs four stages:
1. Checks every credential. It stops with what to fix if Google, Azure or TAHMO access is missing.
2. Runs the four downloads at the same time, each with a log in `$ARCHIVE`:

   | Script | Downloads | Log |
   |---|---|---|
   | `0_download_archives.py` | ECMWF + GEFS | `archives_log.txt` |
   | `0b_download_cumulus.py` | Cumulus | `cumulus_log.txt` |
   | `1_chirps.sh` | CHIRPS | `chirps_log.txt` |
   | `1b_tahmo.sh` | TAHMO | `tahmo_log.txt` |
3. Checks every store is complete (730 ECMWF issuances × 20 years, 1073 GEFS inits × 11 members, 1301 Cumulus inits × 10 members, 45 CHIRPS seasons, TAHMO years) and that values arrived.
4. Prints `all inputs downloaded`, or the problems to fix.

If it stops partway, rerun it: finished pieces are skipped. On a machine that already has the archive it finishes in a few seconds.

**6. Run the analysis:** `bash $HERE/run_all.sh` (see **Quick run**).

### What gets downloaded
The ECMWF store is chunked as whole globes, so reading it from the cloud is the slow part. It is downloaded once for all of East Africa (`resolve-region "East Africa"`), and every later run reads local files. To download a different ECMWF/GEFS box, run `py $HERE/0_download_archives.py $ARCHIVE N/W/S/E` before `download_all.sh`.

| Store | Contents | Time | Size |
|---|---|---|---|
| `$ARCHIVE/ecmwf_reforecast.zarr` | WeatherBench2 IFS extended-range reforecast, daily ensemble mean: every issuance 2016-01-04 to 2022-12-29 (Mon/Thu, 730) × 20 hindcast years (1996–2021 overall) × 46 daily leads, on 31 × 22 cells (1.5°, 27°S–18°N, 21°E–52.5°E). Same variables, units (metres/day) and coordinates as the source. | 15 min | 1.3 GB |
| `$ARCHIVE/gefs_reforecast.zarr` | Sheerwater GEFS reforecast: every Wednesday init 1998-12-30 to 2019-08-28 (1073) × 11 members × 35 daily leads, on 48 × 35 cells (1°, a 1.5° margin around the ECMWF box). mm/day; `init_time` in days since 1960-01-01 (open with `decode_times=False`). Needs the Google credentials above. | 30 s | 0.7 GB |
| `$ARCHIVE/cumulus_reforecast.zarr` | Cumulus (Aurora S2S) precipitation hindcast from Azure `ml_reforecasts/heartbreaking-portfolio/{cf,pf}/total_precipitation_24h_acc/`: every init 2016-01-03 to 2022-12-31 (every 2 days, 1301) × 10 members (0 = control, 1–9 perturbed) × 46 daily steps (interval start), on 1° cells over East Africa plus a 1° margin. `precip` in mm day-1. The source has one global NetCDF per init and lead (about 120,000 files), so it is fetched with 16 threads and cut to the box. The source files carry no units; values are mm per 24 h. | 60 min | 3.4 GB |
| `$ARCHIVE/cumulus_gratis-czajkowski.zarr` | The Cumulus hindcast that matches the live forecast (see **Adding Cumulus**): run `gratis-czajkowski`, 1° IMERG-trained `total_precipitation_24h_acc_imerg`, October–November inits 2016–2021 (186) × 30 members × 46 daily steps, same box and layout as above. Fetched after the first Cumulus store. | 21 min | 1.5 GB |
| `$ARCHIVE/tahmo/tahmo_YYYY.zarr` | TAHMO daily station observations (`precip`, `temperature`, `tmax`, `tmin`, `humidity`, `pressure`) for every TA station in the East African (ICPAC) countries: 330 stations, 186 in Kenya, 61 in Uganda, 47 in Tanzania, 18 in Rwanda and 18 in Ethiopia. Burundi, Djibouti, Eritrea, Somalia, South Sudan and Sudan have none. Stations further south that the East Africa box also covers (Malawi, Zambia, Zimbabwe, Mozambique and others, 131 stations) are left out. One file per year from 2015, plus the station list `stations.csv` (station_id, name, latitude, longitude, country). The current year ends the day before it was downloaded; delete its file to extend it. `TAHMO_COUNTRIES` (ISO codes) and `TAHMO_FIRST_YEAR` change the countries and start year. | 2.1 h (4–14 min a year, growing with the network) | 13 MB |
| `$ARCHIVE/chirps/c15_YYYY.zarr` | CHIRPS final, 1 Oct – 31 Jan of each season 1981–2025 (45 files), daily, regridded conservatively to the 7 × 7 Kenya cells. All 45 seasons should print `ok` in the log. | about 5 min (7 s a season) | 2.5 MB |

Notes:
- **Never downloaded twice:** a finished store, CHIRPS season, TAHMO year or live forecast is skipped without touching the network (a rerun of the ECMWF/GEFS script on a complete archive takes 0.5 s). Delete one to fetch it again.
- **Resuming:**
  - ECMWF (blocks of 50 issuances), GEFS (one member at a time) and Cumulus (one month of inits at a time) save each finished block under `<store>.parts/`. A rerun fetches only the missing blocks, joins them, then deletes the parts.
  - CHIRPS seasons and TAHMO years are written as `tmp_*` and renamed when complete, so a half-written one is redone.
- **Tested:**
  - An archive interrupted at 650 of 730 ECMWF issuances and 8 of 11 GEFS members resumed, fetching only the rest, in 1.7 minutes. The joined stores are identical to an uninterrupted download.
  - A half-written CHIRPS season was redone while the other 44 were skipped.
  - The live forecast was fetched once (21 s) and skipped on the next run (0 s).
- **Live 2026 forecasts:** step 6 saves each ECMWF/GEFS init pair under `$ARCHIVE/live/` and reuses it on later runs.
- **CHIRPS box:** set `CHIRPS_BOX=N/W/S/E` to cover another area. It must reach 0.75° past the outermost cell centres. A large box such as all of East Africa is much slower, because CHIRPS is fetched at 0.05° before regridding.
- **Another region:** to analyse another area inside East Africa, change the box in `prep_hindcasts.py` and `CHIRPS_BOX` to match.
- **Checked:** hindcasts built from these local stores are identical to ones read from the cloud (maximum difference 0.0).

## Quick run (after the downloads)
```bash
bash $HERE/run_all.sh
```
It runs steps 2–8 below in order, prints the time of each, and writes the maps and score tables to `$OUT`.

Timings on 20 Oct 2026 (Apple silicon; network only for step 6):

| Step | First run (cold `uv` environments) | Later runs |
|---|---|---|
| 2 Hindcasts (local) | 12 s | 4 s |
| 3 Onset events | 7 s | 6 s |
| 4 Climatology | 1 s | 0 s |
| 5 Cross-validation | 21 s | 23 s |
| 6 Live 2026 forecasts | 55 s | 21 s |
| 7 Final fits + blends | 17 s | 8 s |
| 8 Maps | 9 s | 8 s |
| **Total** | **122 s** | **70 s** |

Both runs gave the same scores and the same 2026 probabilities as the original cloud-based run. To change the 2026 inits, set `EC_INIT` and `GEFS_INIT` before `run_all.sh` (see step 6).

## Data sources
| Data | Source | Notes |
|---|---|---|
| ECMWF hindcasts | `gs://weatherbench2/datasets/ifs_extended_range/daily/ifs-ext-reforecast-full-single-level-avg_ens_mean.zarr` (public, `token="anon"`) | 2019 model cycle, issued on Thursdays 3 Oct – 28 Nov. Hindcast years 1999–2018 (`hindcast_year` −20..−1; the 2-D `time` coord holds real init dates). `total_precipitation_24hr` is in metres; multiply by 1000. Lead *d* covers day [init + d, init + d + 1). 1.5° grid, longitude 0–358.5. |
| GEFS reforecast | `gs://sheerwater-datalake/caches/gefs_chc_raw/global1_0_precip.zarr` (private) | **Zarr v3**: open with `zarr_format=3, consolidated=False, decode_times=False`. `init_time` is days since 1960-01-01, Wednesdays only, 1998-12-30 to 2019-08-28. 11 members, leads 0–34 days, mm/day, 1.0°. |
| CHIRPS | dynamical.org `ucsb-chc-chirps-analysis-final` (1981 – about a month ago); `-preliminary` for recent days | Variable `precipitation_surface`. Rename it to `precip` for the `icpac-onset` alias. |
| Live ECMWF | dynamical.org `ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree` (inits from 2026-01-01) | 101 members. |
| Live GEFS | dynamical.org `noaa-gefs-forecast-35-day` (inits from 2020-10-01) | 31 members, 0.25°. |
| Analog years | The 2026 El Niño analog set | 1982 1994 1997 2006 2015 2019 2023. Hard-coded in `clim.py` (`ANALOGS`). |

Other places to look:
- **ECMWF on dynamical.org:** the 46-day forecasts only start in 2026, and the 15-day 0.25° ones in April 2024. Neither is long enough for training.
- **Sheerwater ECMWF ensemble-mean weekly store:** the same hindcasts also sit in `gs://sheerwater-datalake/caches/ifs_extended_range/`. That store is chunked as whole globes, about 6 GB to read for Kenya, so this runbook uses the WeatherBench2 copy, about 1 GB.

## Grid and region
- The analysis runs on the global 1.5° grid, cell centres at multiples of 1.5°: latitude −4.5..4.5 and longitude 33..42, so 7 × 7 cells. It is a subset of the East Africa archive.
- The box includes parts of Uganda, Tanzania, South Sudan, Ethiopia and Somalia, plus ocean cells (NaN in CHIRPS).
- Fine data are fetched over `5.25/32.25/-5.25/42.75` so conservative regridding covers whole 1.5° cells.
- GEFS is regridded conservatively onto the ECMWF grid with `coarsen --reference-grid`.

## Steps

The steps below are what `run_all.sh` does, for running or debugging one at a time.

### 1. CHIRPS seasons (one-time; see **Get everything downloaded**)
`1_chirps.sh` writes `$ARCHIVE/chirps/c15_YYYY.zarr`. `run_all.sh` copies them into `$WORK/chirps`, where step 3 writes its event files. When running steps by hand, copy them yourself:
```bash
mkdir -p $WORK/chirps && cp -R $ARCHIVE/chirps/c15_*.zarr $WORK/chirps/
```

### 2. Hindcasts (seconds from the local archive)
```bash
py $HERE/prep_hindcasts.py $WORK     # reads $ARCHIVE; with ARCHIVE unset it reads the cloud stores (about 10 min)
```
Writes `ecmwf_hind.zarr` and `gefs_hind_1deg.zarr`. Both have dims `(time=init, step=0/7/14/21 days, latitude, longitude)`, hold weekly mean mm/day and are stamped `aggregation_period = 7 day`.

GEFS is paired with ECMWF by valid date. For each ECMWF Thursday init, it takes the latest GEFS Wednesday init at least one day earlier (a lag of 1–7 days) and averages GEFS leads [lag + 7k, lag + 7k + 7). Expect 180 ECMWF inits, 177 paired GEFS inits and 3 unpaired. Then regrid GEFS:
```bash
cd $WORK && uv run -q $SKILLS/coarsen/scripts/coarsen.py -i gefs_hind_1deg.zarr --reference-grid ecmwf_hind.zarr --method conservative -o gefs_hind.zarr
```
Sanity check: the correlation between the two models' weekly rain was 0.70 / 0.58 / 0.48 / 0.48 for weeks 1–4. Area means were about 3.5–4.1 mm/day for ECMWF and 2.7–3.0 for GEFS.

### 3. Observed onset events (after step 1; about 10 s)
```bash
py $HERE/events.py $WORK
```
For each season it runs `indicator --rule icpac-onset --first-in 7d -v precipitation_surface`, giving `chirps/ev_YYYY.zarr`, then concatenates 1999–2018 into `$WORK/obs_event.zarr` with `time` 1999-10-01 … 2019-01-31. `--first-in` counts from the first date of each file, which is 1 October, so run it per season and never on a multi-season series.

### 4. Climatology predictor (after steps 2 and 3)
```bash
py $HERE/clim.py $WORK
```
Writes three files:
- `clim_hind.zarr`: each training init uses every 1981–2025 season except its own.
- `clim_2026_analog.zarr`: the analog seasons, for the 2026 init.
- `clim_2026_all.zarr`: all seasons, for the 2026 init.

Each value is P(first onset in the lead week | none yet), taken across seasons on the same day of the season. Edit `ANALOGS` and the 2026 init date in `clim.py` for another year.

### 5. Cross-validation (about 25 s)
```bash
py $HERE/cv.py $WORK
```
For each of the 20 seasons it fits on the other 19 with `mme-fit`, blends the held-out season with `mme-blend`, and scores against `obs_event.zarr`. It runs three predictor sets: `models` (ECMWF, GEFS), `clim`, and `models+clim`. It prints a Brier score table and writes `cv_predictions.csv`. Copy both to `$OUT`.

### 6. Live 2026 forecasts
```bash
EC_INIT=2026-10-15 GEFS_INIT=2026-10-14 bash $HERE/3_forecasts_2026.sh
```
Use a Thursday ECMWF init and the Wednesday GEFS init the day before, to match training. Find the latest inits with `$SKILLS/dynamical-fetch/scripts/fetch.py --probe-latest <dataset id>`. Writes `f2026/ecmwf_wk_p.zarr` and `f2026/gefs_wk.zarr`.

### 7. Final fits and 2026 blends
```bash
bash $HERE/4_fit_blend.sh
```
Writes `w_models.zarr`, `w_models_clim.zarr`, `p2026_models.zarr` and `p2026_models_analog.zarr`.

`mme-blend` matches forecasts to weights by name (`--names ecmwf,gefs[,clim]`) and by `step`. The ECMWF file has a fifth week, which the inner join drops.

### 8. Maps
```bash
bash $HERE/5_plot.sh
```
One panel per lead week. Grey cells are NaN. On the analog map that means every analog season had already had its onset by that week, so the conditional probability is undefined.

## Results from the first run (20 October 2026)
**Weights (log-odds; forecast coefficients per mm/day of weekly mean rain):**

| Week | Models: intercept, ECMWF, GEFS | Models + climatology: intercept, ECMWF, GEFS, climatology |
|---|---|---|
| 1 | −3.59, 0.338, 0.050 | −3.84, 0.296, 0.028, 3.52 |
| 2 | −3.66, 0.276, 0.135 | −3.81, 0.231, 0.099, 3.41 |
| 3 | −3.78, 0.327, 0.095 | −3.86, 0.235, 0.077, 3.79 |
| 4 | −4.05, 0.386, 0.126 | −4.07, 0.250, 0.141, 3.58 |

**Cross-validated Brier skill score against the base rate, weeks 1–4** (corrected 21 Oct 2026; earlier versions of this table compared forecasts and observations by array position and had north and south flipped for the ECMWF/GEFS/climatology rows):

| Predictors | Week 1 | Week 2 | Week 3 | Week 4 |
|---|---|---|---|---|
| Models | +0.12 | +0.10 | +0.08 | +0.09 |
| Climatology | +0.07 | +0.06 | +0.07 | +0.07 |
| Models + climatology | **+0.15** | **+0.12** | **+0.10** | **+0.11** |

Every set beats the base rate at every lead. Adding the climatology term to the two models helps.

**2026 area-mean P(onset in week | none before), init 15 Oct:**

| Predictors | Week 1 | Week 2 | Week 3 | Week 4 |
|---|---|---|---|---|
| Models | 0.09 | 0.09 | 0.14 | 0.15 |
| Models + analog climatology | 0.29 | 0.08 | 0.24 | 0.26 |

If your rerun on the same data and inits gives different numbers, find out why before reporting new ones.

## Adding Cumulus as a third model
The Cumulus (Aurora S2S) hindcast covers 2016–2022, so it overlaps the other training data for only a few seasons. Run these after `run_all.sh`, in the same `$WORK`:

```bash
py $HERE/prep_cumulus.py $WORK      # Cumulus weekly predictors on the ECMWF inits
uv run -q $SKILLS/coarsen/scripts/coarsen.py -i $WORK/cumulus_hind_1deg.zarr --reference-grid $WORK/ecmwf_hind.zarr \
  --method conservative -o $WORK/cumulus_hind.zarr
py $HERE/cv.py $WORK cumulus        # ECMWF + GEFS + Cumulus on the seasons all three share
```

**Pairing:** for each ECMWF Thursday init, `prep_cumulus.py` takes the latest Cumulus init on or before it. Cumulus runs every 2 days, so the lag is 0 or 1 day. It averages the 10-member mean over the same valid-date weeks.

**Longer overlap without GEFS.** GEFS limits the overlap: its reforecast ends in August 2019. The 2022 ECMWF model cycle's hindcasts run to 2021, so ECMWF + Cumulus can share six seasons. Use a separate `$WORK`:
```bash
ECMWF_CYCLE=2022 py $HERE/prep_hindcasts.py $WORK2   # 2002-2021 hindcasts; then coarsen GEFS, prep_cumulus.py, coarsen,
py $HERE/events.py $WORK2 && py $HERE/clim.py $WORK2  # events.py and clim.py as above
py $HERE/cv.py $WORK2 cumulus-no-gefs
```
`ECMWF_CYCLE` picks the issuance year whose 20 hindcast years are used (default 2019: 1999–2018; 2022: 2002–2021). Every predictor set inside one `cv.py` run is scored on the same inits.

**How much the overlap shrinks the training set:**

| Predictors | Inits | Seasons | Onset events, weeks 1–4 |
|---|---|---|---|
| ECMWF + GEFS (2019 cycle) | 177 | 20 (1999–2018) | 389 / 368 / 295 / 253 |
| ECMWF + GEFS + Cumulus | 24 | 3 (2016–2018) | 42 / 45 / 27 / 27 |
| ECMWF + Cumulus (2022 cycle) | 48 | 6 (2016–2021) | 105 / 74 / 65 / 57 |

**Cross-validated Brier skill score against the base rate, weeks 1–4** (corrected scoring, 21 Oct 2026):

| Predictors | Shared seasons | Week 1 | Week 2 | Week 3 | Week 4 |
|---|---|---|---|---|---|
| ECMWF + GEFS | 2016–2018 | +0.06 | +0.02 | −0.03 | +0.00 |
| ECMWF + GEFS + Cumulus | 2016–2018 | +0.08 | +0.02 | −0.02 | −0.04 |
| ECMWF + Cumulus | 2016–2018 | +0.07 | +0.09 | −0.01 | −0.00 |
| Cumulus alone | 2016–2018 | +0.03 | +0.08 | +0.00 | −0.10 |
| ECMWF alone | 2016–2021 | +0.11 | +0.06 | +0.08 | +0.08 |
| ECMWF + Cumulus | 2016–2021 | **+0.14** | **+0.07** | +0.06 | +0.05 |
| Cumulus alone | 2016–2021 | +0.13 | +0.05 | +0.03 | −0.04 |

How to read it:
- **On 2016–2021, ECMWF + Cumulus is best at weeks 1–2**; ECMWF alone holds up best at weeks 3–4, where Cumulus alone falls below the base rate.
- **On 2016–2018 (with GEFS) the differences are within noise:** 32–55 events per lead week.
- **These are 3–6 seasons with 27–105 events per week.** One season such as 2019 can swing the ranking. Treat them as a first look, not a verdict.

### Which Cumulus hindcast matches the live forecast
The live forecast that `cumulus-fetch` reads is run `utmost-plane` (checkpoint `007000`). It is **not** the `heartbreaking-portfolio` hindcast used above:

| Run | Kind | Precipitation products | Members | Inits |
|---|---|---|---|---|
| `utmost-plane` | live | `_era5`, `_imerg` (1°), `_imerg_0p25` | 30 | 2026-09-18 onward |
| `gratis-czajkowski` | hindcast | `_era5`, `_imerg` (1°), `_imerg_0p25` | 30 | 2016–2024, every 2 days |
| `heartbreaking-portfolio` | hindcast | one untagged field | 10 | 2016–2022 |
| `supposed-ballism` / `wakeful-singularity` | live / hindcast (older pair) | `_era5`, `_imerg` | | |

- **Probable match:** `gratis-czajkowski` has the same outputs and member count as the live run.
- **Not provable:** neither hindcast folder records its checkpoint, so the match can't be proven from the data; ask the Cumulus team to confirm.
- **How different the runs are:** on the same 48 inits, the two hindcasts' weekly rain correlates at 0.86–0.89, with near-equal means. Related, but not the same model.

**So weights for the live forecast are fitted on `gratis-czajkowski`,** using its 1° IMERG-trained field to match the live `precip_1deg` product. `download_all.sh` fetches its October–November inits for 2016–2021 into `$ARCHIVE/cumulus_gratis-czajkowski.zarr` (186 inits × 30 members, 21 min, 1.5 GB).

### 2026 forecast with Cumulus (2016–2021 weights)
In a `$WORK2` built with `ECMWF_CYCLE=2022` (see above), after `run_all.sh` has cached the live ECMWF forecast:
```bash
CUMULUS_STORE=cumulus_gratis-czajkowski.zarr py $HERE/prep_cumulus.py $WORK2
uv run -q $SKILLS/coarsen/scripts/coarsen.py -i $WORK2/cumulus_hind_1deg.zarr --reference-grid $WORK2/ecmwf_hind.zarr \
  --method conservative -o $WORK2/cumulus_hind.zarr
py $HERE/cv.py $WORK2 cumulus-no-gefs
WORK=$WORK2 bash $HERE/3b_cumulus_2026.sh            # live Cumulus, 30 members, cached in $ARCHIVE/live/
cd $WORK2
uv run -q $SKILLS/mme-fit/scripts/mme_fit.py --forecast cumulus_hind.zarr --names cumulus \
  --obs obs_event.zarr --model logistic --fit-along step -o w_cumulus.zarr
uv run -q $SKILLS/mme-fit/scripts/mme_fit.py --forecast ecmwf_hind.zarr --forecast cumulus_hind.zarr --names ecmwf,cumulus \
  --obs obs_event.zarr --model logistic --fit-along step -o w_ecmwf_cumulus.zarr
uv run -q $SKILLS/mme-blend/scripts/mme_blend.py --forecast f2026/cumulus_wk.zarr --names cumulus \
  --weights w_cumulus.zarr -o p2026_cumulus.zarr
uv run -q $SKILLS/mme-blend/scripts/mme_blend.py --forecast $ARCHIVE/live/ecmwf_2026-10-15_gefs_2026-10-14/ecmwf_wk_p.zarr \
  --forecast f2026/cumulus_wk.zarr --names ecmwf,cumulus --weights w_ecmwf_cumulus.zarr -o p2026_ecmwf_cumulus.zarr
```

**Cross-validation, 2016–2021, matched hindcast.** Brier skill score against the base rate, weeks 1–4 (`heartbreaking-portfolio` result in brackets):

| Predictors | Week 1 | Week 2 | Week 3 | Week 4 |
|---|---|---|---|---|
| Cumulus alone | +0.14 (+0.13) | +0.07 (+0.05) | −0.01 (+0.03) | −0.08 (−0.04) |
| ECMWF + Cumulus | **+0.15** (+0.14) | **+0.09** (+0.07) | +0.04 (+0.06) | +0.02 (+0.05) |
| ECMWF alone | +0.11 | +0.06 | **+0.08** | **+0.08** |

**Weights fitted on all six seasons (log-odds per mm/day of weekly rain):**

| Week | Cumulus: intercept, coefficient | ECMWF + Cumulus: intercept, ECMWF, Cumulus |
|---|---|---|
| 1 | −3.80, 0.523 | −4.05, 0.216, 0.365 |
| 2 | −3.77, 0.481 | −4.11, 0.219, 0.344 |
| 3 | −3.45, 0.380 | −3.73, 0.271, 0.192 |
| 4 | −3.53, 0.385 | −4.13, 0.426, 0.164 |

Each lead used 910–1,185 samples, and every fit converged in 6 iterations.

**2026 area-mean P(onset in week | none before), init 15 Oct 2026:**

| Predictors | Week 1 | Week 2 | Week 3 | Week 4 | Cell range |
|---|---|---|---|---|---|
| Cumulus | 0.07 | 0.07 | 0.07 | 0.13 | 0.02–0.49 |
| ECMWF + Cumulus | 0.08 | 0.08 | 0.12 | 0.19 | 0.02–0.90 |
| ECMWF alone (2022-cycle weights, 2002–2021) | 0.10 | 0.10 | 0.16 | 0.17 | 0.03–0.84 |

The maps are `p2026_cumulus.png` and `p2026_ecmwf_cumulus.png`. Both blends put the highest chances in the south-west (Lake Victoria basin) and, by week 4, in central and south-eastern Kenya.

**Caveats for these blends:**
- **Live vs hindcast ensembles:** the live ECMWF forecast comes from the current model cycle (101-member mean), but the weights come from 2022-cycle hindcasts (11-member mean).
- **Cumulus member counts match:** 30 members in both the live forecast and `gratis-czajkowski`.
- **Few seasons:** six seasons is still a small training set.
- **Cells already past onset:** these are not masked, as before.

## Known limitations
- **Climatology mismatch:** training uses the leave-one-out all-years climatology, but the 2026 analog blend applies the analog climatology. In cross-validation the (all-years) climatology term improves the blend, but the analog version applied in 2026 was not itself cross-validated, so treat the analog blend as a scenario.
- **Hindcast vs live ensembles:** the hindcasts are an 11-member ECMWF mean and an 11-member GEFS mean. The live forecasts are a 101-member and a 31-member mean.
- **Lag:** GEFS hindcasts are 1–7 days older than the ECMWF init they pair with. In 2026 the lag is 1 day.
- **Cells already past onset:** onset status for the current season can't be confirmed until 21 days after the rain trigger, and the 2026 maps don't mask cells that may already be past onset. By 15 Oct 2026, CHIRPS preliminary showed 13 of 41 cells meeting the 3-day ≥ 20 mm trigger. Check with the CHIRPS preliminary run for 1 Oct to the latest day.
- **Base-rate benchmark:** the base rate is the event frequency over all held-out samples, which is slightly generous.

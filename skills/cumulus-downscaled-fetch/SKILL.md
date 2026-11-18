---
name: cumulus-downscaled-fetch
description: "Fetch the Cumulus AI downscaled precipitation forecast (the Aurora S2S run, or IFS, downscaled to 0.1° over East and West Africa, 46 daily leads; diffusion, flow-matching or UNet model, or the bilinear baseline; ensemble mean or 10 members) for one init date and optional region from a private Azure Blob container, as a weather-skills Zarr. Needs an Azure read token (AZURE_STORAGE_SAS_TOKEN). Use when a task needs the high-resolution Cumulus forecast over Africa."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private NetCDF from Azure Blob italynorthdata/data/live_forecasts/downscaled_models via adlfs. Requires AZURE_STORAGE_SAS_TOKEN (a read SAS with list and read, sp=rl).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.6"
  catalog-group: fetchers
  variables: "precip"
  required-env: "AZURE_STORAGE_SAS_TOKEN"
---

# cumulus-downscaled-fetch

Fetches one Cumulus downscaled init (all leads, ensemble mean or members) and writes daily precipitation rates on a 0.1° grid over 26°N–11°S, 20°W–57°E.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--source SUBFOLDER] [--model FOLDER|SUBSTRING] [--checkpoint SUBSTRING] [--target imerg|era5|SUBSTRING] [--members] [--date YYYY-MM-DD] [--bbox N/W/S/E] -o OUT.zarr
# Latest init date with output (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--source SUBFOLDER] --probe-latest [MODEL]
# The live-models table, sources, inits, and the models, checkpoints and targets at one init (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--source SUBFOLDER] [--date YYYY-MM-DD] --list
```

### Arguments
- `--source` — subfolder of `downscaled_models/` to read: its full name, or a substring that matches only one. Default: `aurora`. `ifs` holds the IFS forecast downscaled the same way. Each subfolder has its own inits, models and checkpoints, so `--probe-latest` and the defaults are worked out per `--source`. An unknown subfolder exits 1 and lists the ones there.
- `--model` — model folder name, or a substring that matches one. Default: the `default` row of `MODELS_LIVE.md` (see Notes) for `--source`, which also fills `--checkpoint` and `--target` when they are not passed; a source with no `default` row exits 1 and lists its rows. The short names `diffusion`, `flow-matching` and `unet` match folders starting `model_diffusion_`, `model_flow_matching_` or `model_unet_`; where several variants run side by side they match more than one folder, and a `--checkpoint` that only one of them has picks it; otherwise the skill exits 1, so pass the args of a `MODELS_LIVE.md` row instead. `bilinear` reads the bilinear-interpolation baseline, which has no checkpoint level.
- `--target` — which precipitation of the source forecast was downscaled: a target folder name as `--list` prints it, or a substring of one such as `imerg` (Aurora's IMERG-trained precipitation) or `era5` (Aurora's ERA5-trained precipitation). Default: the only target folder the checkpoint has, else `imerg`. A value that matches none or several exits 1 and lists what the checkpoint has.
- `--members` — fetch the individual members instead of the ensemble mean. A model with one field (UNet, bilinear) exits 1.
- `--checkpoint` — substring of the checkpoint folder. When an init has several and this is omitted, the skill keeps the one later inits carry on running and warns; if that does not settle it, it exits 1 and lists them.
- `--date` — init date. Default: the latest init with output for `--model`. A date with no output exits 1.
- `--bbox` — `N/W/S/E` decimal degrees inside the domain. Omit for the full domain (about 50 MB per init for the mean, ten times that with `--members`).
- `--variable`, `-v` — the product holds one field, written as `precip`. Accepts `precip`, `tp`, `precipitation`, `total_precipitation` or `precip_24h`; anything else exits 2. Optional.
- `--workers` — maximum concurrent file downloads, at least 1. Default: 16.
- `--probe-latest` — print the latest init date (`YYYY-MM-DD`, or `none`) with output on stdout and exit. Optional value: a `--model` name.
- `--list` — print `MODELS_LIVE.md`, the `--source` subfolders, the inits of `--source`, and each model folder, checkpoint and target at `--date` (default: the latest init) on stdout, then exit. Use it once, before a fetch, when the model to use is not the default or its name is not known; do not guess names to read them from errors.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `precip` in `mm day-1`, dims `(step, latitude, longitude)`, or `(number, step, latitude, longitude)` with `--members`; scalar `time` = the 00 UTC init.
- `step` is the start of each 24 h period: `step = 0` is `[init, init + 1 day)`, up to 46 leads. Already a rate: do not run `deaccumulate`.
- `number` is 0–9; members are interchangeable draws (member 0 is not a control).
- `weather_skills_source=cumulus-downscaled:<source>/<model folder>/<checkpoint>/<target folder>`, `cumulus_downscaled_member_set` (`ensemble_mean` or `members`), `data_interval` `1 day`.

### Notes
- `downscaled_models/MODELS_LIVE.md`, kept by the data provider and printed by `--list`, lists the live models: each row's source, status, first init, and the args to pass here (add `--date`). Unless asked for a specific model, use the `default` row (what a fetch without `--model` reads). If its init is missing or has fewer than 46 leads, use the `fallback` row of the same source and say so. Use `other` rows only when asked. Never present a `baseline` row as the forecast: it is what a model is compared against.
- The downscaling models were trained on ERA5 and are applied to one Aurora run (its control forecast). Expect some bias, and members that differ only by downscaling noise, so the ensemble is underdispersed: do not read member spread as forecast uncertainty.
- The container is private. Set `AZURE_STORAGE_SAS_TOKEN` (a read SAS with list and read, `sp=rl`) before the first call, including `--probe-latest`. A leading `?` or a full blob URL is accepted. Never print the value. When it is not set the workspace has no Cumulus access: say so and use another forecast instead of retrying.
- Each model and checkpoint folder is a separate downscaling setup, and some inits have several. stderr prints `Checkpoint:`, and `weather_skills_source` records it.
- Backfilled inits run today's models on past Aurora forecasts. When the checkpoint differs from the one the latest init runs, stderr warns. When comparing inits across setups, tell the user which inits used which checkpoint. For a consistent multi-init dataset, pass one `--checkpoint` for every init.
- An init still being written can lack leads: a missing lead stays NaN (never filled), steps end at the last published lead, and stderr warns. Pass an earlier `--date` for all 46 leads.
- For the global, coarser Cumulus forecast use `cumulus-fetch`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -o cumulus_ds_kenya.zarr
```
Writes the latest init's ensemble-mean daily precipitation rates from the `MODELS_LIVE.md` default model over Kenya for 46 days.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --model bilinear --date 2026-09-01 --bbox 14/2/4/15 -o cumulus_ds_bilinear_nigeria.zarr
```
Writes the bilinear baseline (Aurora IMERG precipitation interpolated to 0.1°) for the 2026-09-01 init over Nigeria, to compare a model against.

## Details

Source: `az://italynorthdata/data/live_forecasts/downscaled_models/`. Each 24 h total is relabelled to its period start and divided by one day to give a `mm day-1` rate.

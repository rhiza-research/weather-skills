---
name: cumulus-downscaled-fetch
description: "Fetch the Cumulus AI downscaled precipitation forecast (the Aurora S2S run downscaled to 0.1° over East and West Africa, 46 daily leads; diffusion, flow-matching or UNet model; ensemble mean or 10 members) for one init date and optional region from a private Azure Blob container, as a weather-skills Zarr. Needs an Azure read token (AZURE_STORAGE_SAS_TOKEN). Use when a task needs the high-resolution Cumulus forecast over Africa."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads private NetCDF from Azure Blob italynorthdata/data/live_forecasts/downscaled_models/aurora via adlfs. Requires AZURE_STORAGE_SAS_TOKEN (a read SAS with list and read, sp=rl).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
  openclaw:
    requires:
      env:
        - AZURE_STORAGE_SAS_TOKEN
    primaryEnv: AZURE_STORAGE_SAS_TOKEN
    envVars:
      - name: AZURE_STORAGE_SAS_TOKEN
        description: Read SAS (list and read, sp=rl) for Azure container italynorthdata/data
---

# cumulus-downscaled-fetch

Fetches one Cumulus downscaled init (all leads, ensemble mean or members) and writes daily precipitation rates on a 0.1° grid over 26°N–11°S, 20°W–57°E.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--model diffusion|flow-matching|unet] [--target era5|imerg] [--members] [--date YYYY-MM-DD] [--bbox N/W/S/E] -o OUT.zarr
# Latest init date with output (no download)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [MODEL]
```

### Arguments
- `--model` — downscaling model. Default: `diffusion`. Any other model folder name under an init also works, e.g. older runs.

  | `--model` | Model | Output | Inits from |
  | --- | --- | --- | --- |
  | `diffusion` | diffusion UNet | ensemble mean + 10 members | 2026-10-04 |
  | `flow-matching` | flow-matching UNet | ensemble mean + 10 members | 2026-10-27 |
  | `unet` | deterministic UNet | one field | 2026-10-04 |
- `--target` — precipitation product the model was trained to reproduce: `era5` or `imerg`. Default: `era5`.
- `--members` — fetch the individual members instead of the ensemble mean. A model with one field (`unet`) exits 1.
- `--checkpoint` — substring of the checkpoint folder, e.g. `n20_cfg2`. When an init has several and this is omitted, the skill keeps the one later inits carry on running and warns; if that does not settle it, it exits 1 and lists them.
- `--date` — init date. Default: the latest init with output for `--model`. A date with no output exits 1.
- `--bbox` — `N/W/S/E` decimal degrees inside the domain. Omit for the full domain (about 50 MB per init for the mean, ten times that with `--members`).
- `--variable`, `-v` — the product holds one field, written as `tp`. Accepts `tp`, `precip`, `precipitation`, `total_precipitation` or `precip_24h`; anything else exits 2. Optional.
- `--workers` — maximum concurrent file downloads, at least 1. Default: 16.
- `--probe-latest` — print the latest init date (`YYYY-MM-DD`, or `none`) with output on stdout and exit. Optional value: a `--model` name.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- `tp` in `mm day-1`, dims `(step, latitude, longitude)`, or `(number, step, latitude, longitude)` with `--members`; scalar `time` = the 00 UTC init.
- `step` is the start of each 24 h period: `step = 0` is `[init, init + 1 day)`, up to 46 leads. Already a rate: do not run `deaccumulate`.
- `number` is 0–9; members are interchangeable draws (member 0 is not a control).
- `weather_skills_source=cumulus-downscaled:<model folder>/<checkpoint>/<target folder>`, `cumulus_downscaled_member_set` (`ensemble_mean` or `members`), `data_interval` `1 day`.

### Notes
- The container is private. Set `AZURE_STORAGE_SAS_TOKEN` (a read SAS with list and read, `sp=rl`) before the first call, including `--probe-latest`. A leading `?` or a full blob URL is accepted. Never print the value. When it is not set the workspace has no Cumulus access: say so and use another forecast instead of retrying.
- The model and checkpoint folder names are the downscaling setup; any change in them is a change in setup. The live pipeline moves to better checkpoints over time and keeps older ones running for evaluation, so some inits have several. `cfg` (guidance weighting: `cfg2`, `cfg3`) changes the patterns visibly, and `cfg2` is the live one; sampler steps (`n20`, `n30`) are close enough to compare. stderr prints `Checkpoint:`, and `weather_skills_source` records it.
- When the checkpoint differs from the one the latest init runs, stderr warns. Comparing inits across setups is fine (it can even show the effect of a setup), but tell the user which inits used which checkpoint. For a consistent multi-init dataset, pass one `--checkpoint` for every init.
- An init still being written can lack leads: a missing lead stays NaN (never filled), steps end at the last published lead, and stderr warns. Pass an earlier `--date` for all 46 leads.
- For the global, coarser Cumulus forecast use `cumulus-fetch`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -o cumulus_ds_kenya.zarr
```
Writes the latest init's diffusion ensemble-mean daily precipitation rates over Kenya for 46 days.

## Details

Files live at `az://italynorthdata/data/live_forecasts/downscaled_models/aurora/east_africa-ghana-kenya-senegal-west_africa/<init>/<model folder>/<checkpoint>/<target folder>/precip_24h/data/`: `YYYY-MM-DD-00-LLLL.nc` holds the ensemble mean for lead `LLLL` hours (0024 to 1104), and `member_NNN/` holds the same files per member. Each file is the 24 h total ending at its lead; the skill relabels it to the period start and divides by one day to give a rate.

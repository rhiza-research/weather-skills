---
name: cumulus-downscaled-fetch
description: On the first call (including --probe-latest), inject secret AZURE_STORAGE_SAS_TOKEN as an environment variable — do not run once without it and retry. Fetch a Cumulus AI downscaled Aurora precipitation forecast (0.1° over East and West Africa, 26°N–11°S, 20°W–57°E, 46 daily leads; diffusion, flow-matching or UNet model; ensemble mean or its 10 members) for one init date from a private Azure Blob container, as a weather-skills Zarr. Use when a task needs the Cumulus downscaled (high-resolution) forecast. Fetch writes `tp` as a per-step rate (`mm day-1`) — do not run deaccumulate after this skill.
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

Reads one init of the Cumulus AI downscaled forecast: the global Aurora S2S
precipitation forecast downscaled by a machine-learning model to a 0.1° grid
over East and West Africa (latitude 26°N to 11°S, longitude 20°W to 57°E).
Each 24 h lead is one NetCDF file; the skill joins the 46 leads into a
weather-skills Zarr.

Layout:

```
az://italynorthdata/data/live_forecasts/downscaled_models/aurora/east_africa-ghana-kenya-senegal-west_africa/
  <init>/<model folder>/<checkpoint>/<target folder>/precip_24h/data/
    YYYY-MM-DD-00-LLLL.nc              # ensemble mean, LLLL = 0024 … 1104 hours
    member_NNN/YYYY-MM-DD-00-LLLL.nc   # individual members (ensemble models only)
```

By default the skill takes the **most recent** init that has output for the
chosen model and target. Pass `--date` to pin an init.

## When to use

- A task needs the Cumulus downscaled forecast, for example to compare or
  verify it against observations at high resolution.
- For a deterministic evaluation, use the default ensemble mean.

For the global, coarser Cumulus Aurora S2S forecast use `cumulus-fetch`.

## Credentials

The container is private. On the **first** invocation, including
`--probe-latest`, inject `AZURE_STORAGE_SAS_TOKEN` (a SAS query string for
`italynorthdata` / `data`, with list and read — `sp=rl`). A leading `?` or a
full blob URL is accepted; only the query string is used. Never print, log, or
echo the value.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--model NAME] [--target era5|imerg] [--members] [--date YYYY-MM-DD] [--bbox N/W/S/E] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--model NAME] [--target era5|imerg] --probe-latest
```

### Arguments

- `--model` — downscaling model (default `diffusion`):

  | `--model` | Model | Output | Inits from |
  | --- | --- | --- | --- |
  | `diffusion` | diffusion UNet (live) | ensemble mean + 10 members | 2026-10-04 |
  | `flow-matching` | flow-matching UNet | ensemble mean + 10 members | 2026-10-27 |
  | `unet` | deterministic UNet | one field | 2026-10-04 |

  Any other model folder name under an init is also accepted, e.g. older runs.
- `--target` — precipitation product the model was trained to reproduce:
  `era5` (default) or `imerg`.
- `--checkpoint` — substring of the checkpoint folder. Only needed when an init
  has several (the model's sampler settings changed over time, and some inits
  carry two); otherwise the skill uses the one there, warns when there are
  several, and records the one used in `weather_skills_source`.
- `--members` — fetch the individual ensemble members instead of the mean.
  Member labels follow the order the model produced them and carry no meaning.
- `--date` — forecast init date `YYYY-MM-DD`. Default: latest init with output.
- `--probe-latest [model]` — print the latest init `YYYY-MM-DD` with output for
  the model and `--target` on stdout and exit. No `-o`.
- `--bbox` — optional spatial subset `N/W/S/E` inside the domain. Named
  places: compose with `resolve-region`.
- `--variable`, `-v` — only `tp` (aliases `precip`, `precip_24h`).
- `--workers` — max concurrent file downloads (default 16).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A weather-skills standard dataset Zarr with dims `(step, latitude, longitude)`
for the ensemble mean, or `(number, step, latitude, longitude)` with `--members`
(`number` 0..9), and a scalar `time` coord (the 00 UTC init). Source files are
24 h totals; fetch writes `tp` as a per-step **rate** (`mm day-1`),
**left-labeled** so `step = 0` is the first 24 h `[init, init+1d)`. Do **not**
run `deaccumulate`. A lead file that is missing stays NaN and stderr warns.
Next steps are `aggregate-temporal` then `convert-to-totals` for period `mm`.
Stamped with `weather_skills_source=cumulus-downscaled:<model>/<checkpoint>/<target>`
and `cumulus_downscaled_member_set` (`ensemble_mean` or `members`).

The full domain mean is about 50 MB per init; members are ten times that.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. Inspect it with the
`provenance` skill.

## Examples

```bash
# Latest diffusion ensemble mean over Kenya
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -o /tmp/cumulus_ds.zarr
```

```bash
# Members of a past init, IMERG-trained
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-20 --target imerg --members \
    --bbox 5.5/33.5/-5/42.5 -o /tmp/cumulus_ds_members.zarr
```

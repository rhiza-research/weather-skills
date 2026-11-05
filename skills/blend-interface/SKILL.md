---
name: blend-interface
description: "Build an interactive HTML page for choosing blend weights: one map per forecast with a weight slider above it (the sliders always add to 1), the live blend on the right, and a Download blend button that saves the weights as a JSON file to upload to the chat for the blend skill. Use when someone wants to weigh several forecasts by eye before blending."
license: MIT
compatibility: Requires Python 3.12 and uv. Calls the blend skill, which must sit beside it; draws with weather-skills-plotting.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/blend_interface.py *)
metadata:
  version: "0.0.1"
  catalog-group: figure
---

# blend-interface

Writes one self-contained HTML page for choosing weights; the blended Zarr itself comes from blend.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend_interface.py -i A.zarr -i B.zarr [-i ...] -o PAGE.html [--lead N] [--weights W] [--label NAME ...] [-v VAR] [--reference-grid REF.zarr] [--palette NAME] [--title TEXT]
```

### Arguments
- `--input`, `-i`, `--lead`, `--weights`, `--label`, `--variable`, `-v`, `--reference-grid` — as in blend, which this skill runs with `--keep-members`. `--weights` sets where the sliders start. Default: equal weights.
- `--output`, `-o` — the `.html` page.
- `--palette` — a weather-skills-plotting palette (`ppt_week`, `ppt_day`, `ppt_month`, ...). Default: `ppt_week`, for weekly totals in mm.
- `--title` — page title. Default: `Forecast blend, lead N (7 day totals)`.

### Output
An HTML page with plotly.js inside (about 5 MB), so it works offline and in any viewer that runs JavaScript:
- Instructions at the top: set the weights, press **Download blend**, upload the file to the chat.
- A small map per forecast, each with a 0–1 weight slider. Moving one rescales the others in proportion so they add to 1. **Reset to equal weights** undoes it.
- The blend on the right, recomputed as the sliders move with blend's rule (NaN where any weighted forecast is NaN). Hovering it lists every forecast's value in that cell.
- Under the maps, the weights as a dictionary (`{"gefs": 0.5, ...}`, four decimals adding to 1).
- **Download blend** saves `blend_weights_<init>_lead<N>.json`: inputs, variable, lead, init, valid date and weights, the record blend takes as `--weights`. It also posts `{type: "weather-skills-blend", record}` to the embedding page (`window.parent.postMessage`), for hosts that listen.
- The address keeps the current weights after `#w=`, so a reload or shared link opens on them.

### Notes
- To finish a blend: the user uploads the downloaded file to the chat; run blend with the same `-i` files and `--label` values and `--weights <file>`.
- The download lands on the viewer's own computer, not in the agent's sandbox. Where a viewer blocks downloads, the page says so; the weights dictionary under the maps can be selected and pasted into the chat as `--weights` instead.
- The page is about 1,500 px wide; narrower windows cut off the blend and colorbar.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend_interface.py -i gefs_weekly.zarr -i ecmwf_weekly.zarr -i cumulus_weekly.zarr \
  --lead 2 -o blend_week2.html
```
Writes a page for choosing week-2 weights for GEFS, ECMWF and Cumulus, starting from equal weights.

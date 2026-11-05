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
- One line at the top: "When satisfied, either download or copy the blend and give it to the agent."
- A small map per forecast, each with a 0–1 weight slider. Moving one rescales the others in proportion so they add to 1. **Reset to equal weights** undoes it.
- The blend on the right, recomputed as the sliders move with blend's rule (NaN where any weighted forecast is NaN). Hovering it lists every forecast's value in that cell.
- Under the maps, the weights as one line of JSON (`{"gefs":0.5,...}`, four decimals adding to 1), to copy by hand.
- **Download blend** saves that same line as `blend_weights_<init>_lead<N>.json`. It also posts `{type: "weather-skills-blend", record}` (inputs, variable, lead, init, valid date, weights) to the embedding page (`window.parent.postMessage`), for hosts that listen.
- The address keeps the current weights after `#w=`, so a reload or shared link opens on them.

### Notes
- To finish a blend, the user uploads `blend_weights_<init>_lead<N>.json` or pastes the weights line from under the maps. Run blend with the same `-i` files, `--lead` and `--label` values as the page, and `--weights <file or pasted text>`.
- The download lands on the viewer's own computer, not in the agent's sandbox; where a viewer blocks downloads, the page says so and the weights line can be pasted instead.
- The page is about 1,500 px wide; narrower windows cut off the blend and colorbar.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend_interface.py -i gefs_weekly.zarr -i ecmwf_weekly.zarr -i cumulus_weekly.zarr \
  --lead 2 -o blend_week2.html
```
Writes a page for choosing week-2 weights for GEFS, ECMWF and Cumulus, starting from equal weights.

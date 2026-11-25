---
name: blend-interface
description: "Build an interactive HTML page for choosing blend weights, one row per lead (e.g. weeks 1-3): each forecast's map with a vertical 0-1 weight slider beside it (a row's sliders always add to 1), the live blend at the end of the row, and a Download blend button that saves the weights as a JSON file to upload to the chat for the blend skill. Use when someone wants to weigh several forecasts by eye before blending."
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
uv run ${CLAUDE_SKILL_DIR}/scripts/blend_interface.py -i A.zarr -i B.zarr [-i ...] -o PAGE.html [--lead N ...] [--weights W] [--label NAME ...] [-v VAR] [--anomaly A_ANOM.zarr ...] [--palette NAME]
```

### Arguments
- `--input`, `-i`, `--lead`, `--weights`, `--label`, `--variable`, `-v` — as in blend. Each lead is a row of the page; `--weights` sets where the sliders start (and what **Initial** goes back to). Default: lead 1, equal weights.
- `--output`, `-o` — the `.html` page.
- `--anomaly` — an anomaly Zarr for each `-i`, repeated in the same order (e.g. from difference against a climatology). Adds a **Totals / Anomaly** toggle. The anomalies are blended under the same labels and weights, on the totals blend's grid; each must reach a lead exactly when its forecast does.
- `--palette` — a weather-skills-plotting palette (`ppt_week`, `ppt_day`, `ppt_month`, ...). The anomaly view uses its `ppt_anom_` partner (`ppt_anom_week`). Default: `ppt_week`, for weekly totals in mm.

### Output
An HTML page with plotly.js inside (about 5 MB plus the maps), so it works offline in any browser:
- One row per lead: a map per forecast, with a vertical 0–1 weight slider in a strip on its left, then that lead's blend. Moving a slider rescales the others in its row in proportion so they add to 1; every row keeps its own weights. A forecast without a row's lead is greyed out there, its slider off at 0.
- Left of each row, its week and start date, and two buttons for that row only: **Equal** (every forecast with that lead counts the same) and **Initial** (the weights the page was made with).
- Each blend is redrawn as the sliders move with blend's rule (NaN where any weighted forecast is NaN). Hovering a blend shows its value and every forecast's value in that cell above the maps.
- With `--anomaly`, a **Totals / Anomaly** toggle that switches every map and the colorbar; the weights carry over.
- Under the maps: "When satisfied, either download or copy the blend and give it to the agent.", the weights as JSON, a lead per line (`{"week1": {"gefs":0.5,...}, "week2": {...}}`, four decimals adding to 1 per lead), and **Download blend**, which saves that same text as `blend_weights_<init>_week1_week2.json`.

### Notes
- To finish a blend, the user uploads the downloaded file or pastes the weights text. Run blend with the same `-i` files, `--lead` and `--label` values as the page, and `--weights <file or pasted text>`.
- The download lands on the viewer's own computer, not in the agent's sandbox.
- The page is one panel (150–210 px) wider than the forecasts, plus the colorbar: about 1,250 px for four forecasts. The mouse wheel scrolls the page; drag a map to pan it.
- Base-map lines are national borders and coastlines only, which keeps large regions' pages small.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend_interface.py -i gefs_weekly.zarr -i ecmwf_weekly.zarr -i cumulus_weekly.zarr \
  --lead 1 2 3 -o blend_weeks1-3.html
```
Writes a page for choosing weights for GEFS, ECMWF and Cumulus in each of weeks 1 to 3, starting from equal weights.

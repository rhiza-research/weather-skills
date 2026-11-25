---
name: blend
description: "Blend two or more forecasts at one or more leads (e.g. weeks 1-3) into a weighted mean on a common grid, regridding each with coarsen or downscale. Weights per week come from blend-interface's downloaded file or weights text; without them every forecast counts equally. Use for a multi-model blend or multi-model mean."
license: MIT
compatibility: Requires Python 3.12 and uv. To regrid, calls the coarsen and downscale skills (from weather-skills/weather-skills), which must sit beside it, as the weather-skills plugin installs them.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/blend.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# blend

Writes the weighted mean of several forecasts at one or more leads, on one grid.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend.py -i A.zarr -i B.zarr [-i ...] -o OUT.zarr [--lead N ...] [--weights W] [--label NAME ...] [-v VAR] [--reference-grid REF.zarr] [--keep-members]
```

### Arguments
- `--input`, `-i` — a forecast Zarr; repeat, at least twice. Each holds the variable on lat/lon plus at most one lead axis (`time` or `step`); reduce an ensemble first (summarize-dim `--dim number`).
- `--output`, `-o` — output Zarr.
- `--lead` — 1-based positions along the lead axis: `--lead 1 2 3` is weeks 1 to 3 of weekly totals. An input too short for a lead is left out of that lead only, with a note on stderr. Default: `1`.
- `--weights` — blend-interface's weights, as its downloaded `.json` file or the text under its maps: `{"week1": {"gefs": 0.6, "ecmwf": 0.4}, "week2": {...}}`, one entry per lead. A forecast missing from a lead's entry gets 0; one without that lead may only get 0. Each lead's weights are scaled to add to 1. Default: equal weights.
- `--label` — a name per input, in `-i` order; these are the names in `--weights`. Default: the file name up to `_weekly`, `_daily`, `_monthly` or `_init` (`gefs_weekly_totals_init2026-11-02.zarr` → `gefs`).
- `--variable`, `-v` — the variable to blend. Default: the one data variable every input shares.
- `--reference-grid` — Zarr whose lat/lon grid the blend is on. Default: the grid most inputs share (ties: the earliest).
- `--keep-members` — also write each forecast on the blend grid as `<var>_members` along a `model` dim, NaN at a lead it lacks (used by blend-interface).

### Output
- `<var>`: the blend on (lead dim, `latitude`, `longitude`), one entry per `--lead`; the lead dim holds the inputs' own values there, such as each week's start date. Attrs are the first input's, `long_name` ending in ` blend`, plus `blend_weights`.
- `<var>_members` with `--keep-members`.
- Dataset attrs: `blend_leads` (JSON list), `blend_weights` (`{"week1": {label: weight}, ...}`), `blend_left_out` (`{"week3": [labels], ...}`: forecasts without that lead), `weather_skills_forecast_init` from the first input, and `license` listing every input's licence.
- stderr: the weights used and any forecasts left out.

### Notes
- Leads are named `week1`, `week2`, ... when the inputs are weekly (`aggregation_period` `7 day`), else `lead1`, `lead2`, ...
- An input on another grid is regridded by its spacing: a finer one with coarsen `--method conservative` (a target cell partly outside a masked input, such as a country-only field, is NaN), a coarser one with downscale `--algorithm linear-interpolation` (NaN beyond its outermost cell centres).
- A blend cell is NaN wherever any forecast with weight above 0 at that lead is NaN, so the blend never quietly becomes a mean of fewer forecasts.
- Inputs must share units and aggregation period (weekly totals with weekly totals); convert first with unit-convert, aggregate-temporal or convert-to-totals.
- Weights follow labels, not input order: pass the same `-i` files and `--label` values the weights were chosen with.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend.py -i gefs_weekly.zarr -i ecmwf_weekly.zarr -i cumulus_weekly.zarr \
  --lead 1 2 --weights blend_weights_2026-11-02_week1_week2.json -o blend_weeks1-2.zarr
```
Writes the week-1 and week-2 rainfall blends with the weights chosen on blend-interface's page, on the grid two of the forecasts share.

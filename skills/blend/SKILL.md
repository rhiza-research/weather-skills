---
name: blend
description: "Blend two or more forecasts at one lead (e.g. week 2) into a weighted mean on a common grid, regridding each with coarsen or downscale. Weights come from blend-interface's Finish button, a JSON dictionary, or name=value pairs; without them every forecast counts equally. Use for a multi-model blend or multi-model mean."
license: MIT
compatibility: Requires Python 3.12 and uv. Calls the coarsen and downscale skills, which must sit beside it.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/blend.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# blend

Writes the weighted mean of several forecasts at one lead, on one grid.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend.py -i A.zarr -i B.zarr [-i ...] -o OUT.zarr [--lead N] [--weights W] [--label NAME ...] [-v VAR] [--reference-grid REF.zarr] [--keep-members]
```

### Arguments
- `--input`, `-i` — a forecast Zarr; repeat, at least twice. Each holds the variable on lat/lon plus at most one lead axis (`time` or `step`); reduce an ensemble first (summarize-dim `--dim number`).
- `--output`, `-o` — output Zarr.
- `--lead` — 1-based position along each input's lead axis: `1` is the first week of weekly totals. Inputs too short for it are left out, with a note on stderr. Default: `1`.
- `--weights` — one weight per forecast, by label: the record blend-interface's Finish button gives (text or its downloaded `.json`), a JSON dictionary (`'{"gefs": 0.6, "ecmwf": 0.4}'`), or pairs (`gefs=0.6,ecmwf=0.4`). Every forecast that has the lead needs one (`0` leaves it out); one without the lead may only be given `0`. Scaled to add to 1. Default: equal weights.
- `--label` — a name per input, in `-i` order; these are the keys of `--weights`. Default: the file name up to `_weekly`, `_daily`, `_monthly` or `_init` (`gefs_weekly_totals_init2026-11-02.zarr` → `gefs`).
- `--variable`, `-v` — the variable to blend. Default: the one data variable every input shares.
- `--reference-grid` — Zarr whose lat/lon grid the blend is on. Default: the grid most inputs share (ties: the earliest).
- `--keep-members` — also write each blended forecast on the blend grid as `<var>_members` along a `model` dim (used by blend-interface).

### Output
- `<var>`: the blend on (lead dim of length 1, `latitude`, `longitude`); the lead dim holds the first input's value at that lead, such as the week's start date. Attrs are the first input's, `long_name` ending in ` blend`, plus `blend_weights`.
- `<var>_members` with `--keep-members`.
- Dataset attrs: `blend_weights`, `blend_lead`, `blend_left_out` (forecasts without the lead), `weather_skills_forecast_init` from the first input, and `license` listing every input's licence.
- stderr: the weights used and any forecasts left out.

### Notes
- An input on another grid is regridded by its spacing: a finer one with coarsen `--method conservative` (a target cell partly outside a masked input, such as a country-only field, is NaN), a coarser one with downscale `--algorithm linear-interpolation` (NaN beyond its outermost cell centres).
- A blend cell is NaN wherever any forecast with weight above 0 is NaN, so the blend never quietly becomes a mean of fewer forecasts.
- Inputs must share units and aggregation period (weekly totals with weekly totals); convert first with unit-convert, aggregate-temporal or convert-to-totals.
- Weights follow labels, not input order: pass the same `-i` files and `--label` values the weights were chosen with.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/blend.py -i gefs_weekly.zarr -i ecmwf_weekly.zarr -i cumulus_weekly.zarr \
  --lead 2 --weights 'gefs=0.5,ecmwf=0.3,cumulus=0.2' -o blend_week2.zarr
```
Writes the week-2 rainfall blend: half GEFS, 30% ECMWF, 20% Cumulus, on the grid two of them share.

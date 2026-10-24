---
name: downscale
description: "Put a gridded weather-skills Zarr onto a finer or equally spaced lat/lon grid, by linear interpolation or by interpolation plus empirical quantile mapping (q-q) against an observed climatology. The target grid comes from an integer factor, a resolution, or another Zarr's grid. Use when a task needs higher spatial resolution, or a forecast bias-corrected onto an observation grid."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py *)
metadata:
  version: "0.0.4"
  catalog-group: transforms
---

# downscale

Regrids a dataset onto a target lat/lon grid that is finer than, or as fine as, the input, optionally quantile-mapping the values.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i IN.zarr -o OUT.zarr --algorithm linear-interpolation|q-q (--factor N | --target-resolution DEG | --reference-grid REF.zarr) [-v NAME] [--qq-reference OBS_CLIM.zarr [--qq-model MODEL_CLIM.zarr]]
```

### Arguments
- `--input`, `-i` — input Zarr with lat and lon.
- `--output`, `-o` — output Zarr.
- `--algorithm` — required. `linear-interpolation` interpolates onto the target grid. `q-q` interpolates, then maps each value through quantile mapping (see Details).
- `--factor`, `-f` — integer >= 1; splits each input cell into `factor` steps, so every factor-th output point is an input point. `1` keeps the input grid.
- `--target-resolution` — target spacing in degrees.
- `--reference-grid` — Zarr whose lat/lon values become the target, kept to the input's extent plus half an input cell.
- `--variable`, `-v` — process only this data variable. Default: all.
- `--qq-reference` — `q-q` only, required: observed climatology sample. Must contain every output grid point and have a dim besides lat/lon (e.g. `time` over many years).
- `--qq-model` — `q-q` only: the model's own climatology sample (hindcasts, reforecasts) for the same variable, season, and lead, on a grid covering the input. Needs a dim besides lat/lon. Default: the input is its own model sample.

Pass exactly one of `--factor`, `--target-resolution`, `--reference-grid`.

### Output
The input with lat/lon replaced by the target grid; `time`, `step`, `number`, and attrs are kept. With `--factor` or `--target-resolution`, points start at the input's first lat and lon. Longitudes in `[0, 360]` are wrapped to `[-180, 180)`.

### Notes
- A target coarser than the input on either axis exits with an error. Spacings within 1% count as equal, so a same-resolution half-cell shift is accepted.
- For `q-q`, every input variable must exist in `--qq-reference` (and `--qq-model`) under the same name, in the same units, with the same lat/lon dim names.
- Without `--qq-model`, the input is ranked against itself, so a single forecast comes out as the reference climatology (stderr warns). Omit it only when the input is itself a long historical run.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i ecmwf_w2_kenya.zarr -o ecmwf_w2_qq.zarr --algorithm q-q --reference-grid chirps_w2_clim.zarr --qq-reference chirps_w2_clim.zarr --qq-model ecmwf_w2_hindcast.zarr
```
Writes week-2 ECMWF precip on the CHIRPS 0.05° Kenya grid, mapped from the model's week-2 hindcast distribution onto CHIRPS's.

## Details

**Quantile mapping.** Per output cell, each value `x` becomes `F_ref⁻¹(F_model(x))`, where `F_model` and `F_ref` are the cell's empirical distributions over the `--qq-model` and `--qq-reference` samples. A forecast at the model's 90th percentile becomes the observed 90th percentile: the forecast signal stays and the model bias goes. Each sample is every value along the file's non-spatial dims (years, days, members) pooled, so the two files need not share dims with each other or the input. The model sample is interpolated onto the output grid like the input. A value tied with many model values (dry days) takes the middle of the tie block: if the model is dry 60% of the time, a dry forecast maps to the observed 30th percentile.

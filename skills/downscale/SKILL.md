---
name: downscale
description: Downscale a weather-skills standard dataset Zarr onto a finer-or-equal grid, adding information via a chosen --algorithm (linear-interpolation or q-q empirical quantile mapping). q-q on a forecast needs the model's own climatology (--qq-model, e.g. hindcasts) plus an observed climatology on the output grid (--qq-reference); without --qq-model the input is its own model sample, so a single forecast comes out as the reference climatology. The target is given by an integer factor, a target resolution, or a reference dataset's grid. Equal or near-equal resolution is a lateral realign (same spacing, different offset — including a half-cell shift). Use when a task needs higher spatial resolution; to make a grid coarser, use the coarsen skill.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# downscale

Source-agnostic spatial downscaling: produces a grid finer than or equal to
the input's and adds information through a pluggable `--algorithm`. The target grid is
specified one of three ways — an integer `--factor` (new spacing = input
spacing / factor), a `--target-resolution` in degrees, or a `--reference-grid`
dataset whose lat/lon grid becomes the target. The requested target must be
finer-or-equal to the input on each axis; equal (or near-equal) resolution
is accepted as a lateral realign — same spacing, different cell-center
offset, including a half-cell shift. Geometry is a no-op when the grids
already match (for `--algorithm q-q` the value mapping still applies). A
meaningfully-coarser target is rejected with a pointer to the `coarsen`
skill. Near-equal spacings (within 1%, including float rounding or a
slightly irregular CHIRPS axis vs a regular 0.05° forecast) are not treated
as coarser, so this skill and `coarsen` do not ping-pong.

Algorithms:

- `linear-interpolation` — linearly interpolate the input onto the finer target
  grid (via `xarray-regrid`'s `.regrid.linear()` accessor). No distribution
  change.
- `q-q` — linearly interpolate onto the finer target grid, then apply
  per-grid-cell empirical quantile mapping: each value `x` becomes
  `F_ref⁻¹(F_model(x))`. `F_model` is the cell's CDF over the `--qq-model`
  climatology sample, `F_ref` the cell's CDF over the `--qq-reference`
  sample. A forecast at the model's 90th percentile becomes the observed
  90th percentile, so the forecast signal is kept and the model's bias is
  removed. A value tied with many model values (precipitation zeros) takes
  the middle of its tie block, so a dry forecast maps to the observed
  percentile halfway through the model's dry fraction: if the model is dry
  60% of the time and the observations 40%, it maps to the observed 30th
  percentile, which is still dry.

**q-q needs a model climatology.** Pass `--qq-model` with the model's own
climatology for the same variable, season, and lead (hindcasts /
reforecasts, or a long historical run). Without it the input is used as its
own model sample: that is only right when the input **is** a long
historical run. On a single forecast it ranks the forecast against itself
and returns the reference climatology (the forecast signal is lost); the
skill warns on stderr.

**Samples.** Each cell's climatology sample is every value along the
non-spatial dims of `--qq-model` / `--qq-reference` (years, days, members),
pooled — they need not share a dim with each other or with the input, but
each needs at least one non-spatial dim. Select the matching season and
lead before building them. The input keeps all its dims (`step`, `number`,
...); each value is mapped on its own.

## When to use

- A task needs a finer grid, or a lateral realign onto another dataset's
  coordinates before `difference` or `verify`. Drawing them side by side
  does not.
- Matching the (finer) resolution of another dataset via its grid
  (`--reference-grid`).
- Bias-correcting a forecast against observations on the output grid
  (`--algorithm q-q --qq-model <hindcasts.zarr> --qq-reference <obs_clim.zarr>`).

Not for: coarsening a grid onto a strictly-coarser resolution — that is the
`coarsen` skill.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py --input <in.zarr> --output <out.zarr> \
    --algorithm {linear-interpolation,q-q} \
    (--factor N | --target-resolution DEG | --reference-grid REF.zarr) \
    [--variable NAME] \
    [--qq-reference OBS_CLIM.zarr [--qq-model MODEL_CLIM.zarr]]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded dataset).
- `--output`, `-o` — output Zarr.
- `--algorithm` — `linear-interpolation` or `q-q`. Required.
- `--factor`, `-f` — integer refinement factor (>= 1). New spacing = input spacing / factor (factor 1 = identity). Mutually exclusive with `--target-resolution` and `--reference-grid`.
- `--target-resolution` — target spacing in degrees; must be finer-or-equal (<=) to the input on each axis (near-equal counts as equal). Mutually exclusive with `--factor` and `--reference-grid`.
- `--reference-grid` — path to a reference Zarr whose lat/lon grid defines the target (its own dim names, `lat` or `latitude`, are fine). Only its points within half an input cell of the input's extent are used, so a continental reference doesn't produce a mostly empty grid. The reference grid must be finer-or-equal to the input (near-equal counts as equal, so a half-cell offset at the same nominal resolution is allowed). Mutually exclusive with `--factor` and `--target-resolution`.
- `--variable`, `-v` — restrict to a single data variable. Default: process all.
- `--qq-reference` — observed climatology sample: the distribution `q-q`
  maps onto. Must contain every post-downscale grid point (it may extend
  past them, so the same Zarr can serve as `--reference-grid`) and have a
  sample dim besides lat/lon. Required for `--algorithm q-q`.
- `--qq-model` — the model's climatology sample (hindcasts / reforecasts),
  on any grid covering the input; it is interpolated onto the output grid
  the same way as the input. Needs a sample dim besides lat/lon.
  Strongly recommended; see above.

Every input variable must exist under the same name and in the same units
in `--qq-reference` (and `--qq-model`), or the skill errors before
interpolating — `rename` / `unit-convert` one side, or limit the input with
`--variable`.

### Output

Same shape as input except the lat/lon dims are finer. Non-spatial dims
(`number`, `step`, `time`) are preserved. With `--algorithm q-q` every data
variable is mapped.

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Examples

```bash
# Factor-4 finer, linear interpolation.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_4x.zarr \
    --algorithm linear-interpolation --factor 4
```

```bash
# Onto a finer 0.05° grid, linear interpolation.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/imerg.zarr -o /tmp/imerg_p05.zarr \
    --algorithm linear-interpolation --target-resolution 0.05
```

```bash
# Match the (finer) grid of another dataset.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_on_imerg.zarr \
    --algorithm linear-interpolation --reference-grid /tmp/imerg.zarr
```

```bash
# Week-2 ECMWF totals onto the CHIRPS 0.05° grid, q-q mapped from the model's
# week-2 hindcast climatology onto CHIRPS totals for the same calendar week
# (one value per year along time), all with the same variable name.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf_w2.zarr -o /tmp/ecmwf_w2_qq.zarr \
    --algorithm q-q --reference-grid /tmp/chirps_w2_clim.zarr \
    --qq-reference /tmp/chirps_w2_clim.zarr --qq-model /tmp/ecmwf_w2_hindcast.zarr
```

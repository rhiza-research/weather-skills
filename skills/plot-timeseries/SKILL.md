---
name: plot-timeseries
description: Render a PNG with traces overlaid on a shared time axis, as lines or bars. Name files with repeatable -i. Set parameters in --spec: traces[].reduce, traces[].along (e.g. number for ensemble spaghetti), traces[].mark, traces[].line, layout.subplots, layout.bar_mode, title, theme.fontsize (default 16). --dump-spec prints the merged spec. Leftover non-time dims must be reduced or fanned out; nothing is averaged silently. For precipitation, run aggregate-temporal then convert-to-totals first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot-timeseries

Source-agnostic multi-input timeseries plotting. Takes one or more weather-skills
standard dataset Zarrs and draws them on a single set of axes against the
time/step coord. `traces[].mark` `line` (default) is a polyline with a marker at each
time; `bar` is a grouped bar chart (one bar group per time, one bar per
bar-styled input). A per-trace `mark` overrides that choice, so observed
totals can be bars with a climatology drawn as a line.

Each `--input` is one legend series. A leftover non-time dim can be fanned
with `traces[].along` (typically `number` / `member`): every value along that dim
becomes a line. `along_color` `same` shares one color and one legend
entry (ensemble spaghetti). `cycle` paints each value a distinct
color with its own legend entry (analog years concatenated on one dim). That is
how to plot 101 ensemble-member difference traces from a
single Zarr — do not split members into 101 `--input` files (capped at 26
inputs). `along` traces are always lines and may overlay bar-styled inputs.

1D inputs (only a time-like dim left after `inputs[].variable`) plot as-is. Any other
non-time dim must be named in `traces[].reduce` (mean) or `along` (one line per
value). There is no silent averaging, and no reference / climatology overlay
beyond passing a second `--input`.

A forecast input whose axis is `step` (timedelta lead times) plus a scalar
init `time` is plotted against **valid time** (`init + step`) so the x-axis
shows calendar dates, not raw nanoseconds. Run `step-to-time` first if you
need a real `time` dim for other skills (`difference`, `verify`).

For a single-input quick-look, use the `plot` skill with
`traces[0].kind` `timeseries`. Leftover non-time dims are still not averaged: set
`traces[].reduce` once per dim or `along` to fan them out.

## When to use

- Comparing the same variable across two or more datasets (forecast vs
  observation, or two models) as lines or bars, or as `layout.subplots`
  when the y-scales should stay independent.
- Plotting every ensemble member from one forecast Zarr
  (`traces[].along` `number`), optionally with a 1D overlay.
- Highlighting one series (`traces[].line`, matched by `input` id).
- Overlaying a climatology line on observed bars (`traces[].mark` `bar` on
  one input and `line` on the other).
- Plotting a single dataset as a 1D timeseries when you want explicit
  control over which dims are reduced. Period totals often read better as
  `mark` `bar`.

For maps of one dataset over time, use `plot`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py -i <a.zarr> [-i <b.zarr> ...] -o <out.png> \
    --spec '{"inputs":[{"variable":"tp"}],"traces":[{"reduce":["latitude","longitude"],"mark":"line"}],"title":"Forecast"}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py -i <a.zarr> --dump-spec -
```

### Arguments

- `--input`, `-i` — input Zarr; repeat the flag for each series. Order is the legend order. Optional when `--spec` lists input paths.
- `--output`, `-o` — PNG path.
- `--spec` — JSON object or path, always deep-merged onto the spec built from the opened files. Your values win. `inputs[]` merges by id, `traces[]` by `input` (else id). A `patch` key inside the object is rejected.
- `--dump-spec` — write the merged spec as JSON and skip the PNG. Bare `--dump-spec` or `-` prints to stdout.

### Parameters (`--spec`)

One internal trace per input, kind `timeseries`, mark `line`. `layout.bar_mode` defaults to `grouped`. Globals (`reduce`, `along`, `along_color`, `mark`, `align`, `band`, `time_dim`) are read from `traces[0]` and apply to every series. Per-series `line`, `bar`, and `mark` stay on that trace.

- `inputs[].variable`, `inputs[].label`, `title`, `xlabel`, `ylabel`.
- `traces[].reduce` — dims to average. Required for leftover non-time dims unless that dim is `along`.
- `traces[0].along` — fan one leftover dim into lines (`number`). `along_color` is `same` or `cycle`.
- `traces[].mark` — `line` or `bar`. `layout.bar_mode` — `grouped`, `stacked`, or `overlay`.
- `traces[0].align` — `dayofyear` to overlay a seasonal axis. `traces[0].band` — percentile pair, e.g. `[10, 90]`, and it requires `along`.
- `layout.subplots` — one panel per input. `layout.figsize` as `[W, H]`. `theme.fontsize` (default 16), `theme.template`.
- Per-series style: `traces[].line` (`color`, `linewidth`, `linestyle`, `marker`, `markersize`, `alpha`, `zorder`) or `traces[].bar`. Match a series with `traces[].input` (`a`, `b`, …) or by index.

### Output

A PNG at `--output`. Stdout prints `plot hash` (sha256 of RGB pixels) and
`data: not null` or `data: NULL`. Compare hashes across runs; `NULL` means
inspect-zarr the inputs. Look at the PNG as well. `--dump-spec` skips the
PNG and this report.
Overlay mode is a single axes (default `figsize=(10, 6)`).
`layout.subplots` is one stacked panel per input. Override the canvas with
`layout.figsize`. One series per input
(line with markers, `along` spaghetti, or bars; a per-trace `mark` of `line`
can overlay a line on bars), legend below the traces. The y-axis label is the variable `long_name` (then
`GRIB_name`, then the variable name) plus `[<units>]` when the variable
carries a `units` attribute. Units are a short display form (`mm/day`,
`°C`), not the on-disk CF string.

### Input units

In overlay mode, all traces share one y-axis whose label takes the units of the
first input. When the overlaid inputs carry the plotted variable in differing
`units`, series in different units are drawn against a single scale and labeled
with only one of them. The skill prints a warning to stderr naming the distinct
units and still renders (exit status 0); set `layout.subplots` for independent
y-axes. It is a rendering caveat, not a hard error.
Only inputs that carry a `units` attr participate in the comparison.

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata. Read-back:

```bash
python3 -c "from PIL import Image; import json; img = Image.open('out.png'); print(json.loads(img.info['weather_skills_history']))"
```

## Examples

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/chirps.zarr -i /tmp/imerg.zarr -o /tmp/precip_panels.png \
    --spec '{"inputs":[{"variable":"precip","label":"CHIRPS"},{"label":"IMERG"}],"traces":[{"reduce":["latitude","longitude"]}],"layout":{"subplots":true}}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ens_diff.zarr -o /tmp/ens_traces.png \
    --spec '{"inputs":[{"variable":"tp"}],"traces":[{"reduce":["latitude","longitude"],"along":"number","along_color":"same"}],"title":"Ensemble difference traces"}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/obs.zarr -i /tmp/clim.zarr -o /tmp/obs_vs_clim.png \
    --spec '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]},{"input":"b","mark":"line","line":{"linestyle":"--","linewidth":2.5,"marker":"none"}}],"title":"30-day precip vs climatology"}'
```

`--dump-spec -` prints the merge when you need to inspect a key. Edit `--spec` and run again.

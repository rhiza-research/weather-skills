---
name: plot-timeseries
description: Render a single PNG with traces overlaid on a shared time axis, as lines (default) or grouped bars. Pass --subplots for one stacked panel per --input (shared time axis, independent y-scales). Each --input can be 1D already, reduced to 1D via --reduce, or fanned along one leftover dim with --along (e.g. --along number for 101 ensemble members as one spaghetti group — one Zarr, one legend entry, not 101 --input files). Repeatable --trace SELECTOR:k=v styles a series (color, linewidth, marker, zorder, style=line|bar) by 1-based input index, legend label, or a unique token in the label (e.g. 2026). Per-trace style=line|bar overrides global --style so one series can be bars and another a line. Use when you want to compare a variable across datasets or plot ensemble-member traces. Inputs whose variable still has non-time dims after selection must --reduce or --along them; no silent averaging. For precipitation, run aggregate-temporal then convert-to-totals first — plot totals (`mm`), not rates. Use --fontsize to enlarge titles, axis labels, ticks, and legend (default 16).
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
time/step coord. `--style line` (default) is a polyline with a marker at each
time; `--style bar` is a grouped bar chart (one bar group per time, one bar per
bar-styled input). `--trace style=line|bar` overrides that global choice per
series, so observed totals can be bars with a climatology drawn as a line.

Each `--input` is one legend series. A leftover non-time dim can be fanned
with `--along DIM` (typically `number` / `member`): every value along that dim
becomes a line, sharing color and one legend
entry. That is how to plot 101 ensemble-member difference traces from a
single Zarr — do not split members into 101 `--input` files (capped at 26
inputs). `--along` traces are always lines (thin, translucent, no markers
unless `--trace` says otherwise) and may overlay bar-styled inputs.

1D inputs (only a time-like dim left after `--variable`) plot as-is. Any other
non-time dim must be named in `--reduce` (mean) or `--along` (one line per
value). There is no silent averaging, and no reference / climatology overlay
beyond passing a second `--input`.

A forecast input whose axis is `step` (timedelta lead times) plus a scalar
init `time` is plotted against **valid time** (`init + step`) so the x-axis
shows calendar dates, not raw nanoseconds. Run `step-to-time` first if you
need a real `time` dim for other skills (difference, plot-compare).

For a single-input quick-look, use the `plot` skill with
`--style timeseries`, which averages across all non-time dims by default
(no `--reduce` flags needed).

## When to use

- Comparing the same variable across two or more datasets (e.g. forecast vs.
  observation, or two forecast models) as line traces or grouped bars on one
  figure, or as stacked `--subplots` when the y-scales should stay independent.
- Plotting every ensemble member as a spaghetti / difference trace from one
  forecast Zarr (`--along number`), optionally with a 1D overlay (mean, obs).
- Highlighting one input among analog years (`--trace 2026:color=black,linewidth=2.5`).
- Overlaying a climatology line on observed period totals (`--style bar` plus
  `--trace clim:style=line,linestyle=--,linewidth=2.5`).
- Plotting a single dataset as a 1D timeseries when you want explicit
  control over which dims are reduced. Period totals (dekadal/monthly precip)
  often read better as `--style bar`.

For maps of N forecasts (or forecasts vs gridded obs) over time, use
`plot-compare-forecasts`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py -i <a.zarr> [-i <b.zarr> ...] --output <out.png> \
    [--variable NAME] [--time-dim DIM] [--reduce DIM ...] [--along DIM] [--title TEXT] \
    [--xlabel TEXT] [--ylabel TEXT] [--fontsize N] [--figsize W,H] \
    [--style line|bar] [--subplots] [--align-day-of-year] [--band LOW,HIGH] \
    [--theme weather_skills|colorblind] [--trace SELECTOR:k=v ...] \
    [--spec PATH_OR_JSON] [--dump-spec PATH|-|none]

uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py --spec <out.plot.json> --output <out2.png>
```

### Arguments
- `--input`, `-i` — input Zarr; repeat the flag for each input. Order is
  preserved and controls the legend order. Optional when `--spec` already
  lists input paths.
- `--spec` — plot spec JSON (file or inline). A default run writes
  `<output-stem>.plot.json` with resolved inputs, `--reduce`/`--along`,
  `--style`, labels, and the shared matplotlib `axes` object (limits, ticks,
  locators, spines, …). Edit `axes` and re-run with `--spec`. CLI flags
  overlay the spec. Spec input paths are opened as Datasets so provenance
  still chains from the Zarr.
- `--dump-spec` — where to write the resolved plot spec. Default:
  `<output-stem>.plot.json`. `-` prints to stdout; `none` skips the sidecar.
- `--label` — legend label (overlay) or subplot title (`--subplots`) for each
  `--input`, in order. When omitted, labels are inferred from station
  metadata, filename, or provenance.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable of
  the first input. Must exist in every input.
- `--time-dim` — name of the time-like dim. When omitted, `time` is used if
  present, else `step`, else the cf-xarray-identified time axis.
- `--reduce` — name of a non-time dim to average out before plotting.
  Repeatable: pass once per dim to reduce. Required when an input's variable
  has any non-time dims after variable selection (unless that dim is named
  in `--along`); the skill exits with an error rather than silently averaging.
- `--along` — name of one leftover non-time dim to fan into traces (e.g.
  `number`, `member`, `realization`). One `--input` yields many lines, one
  legend entry, shared color. Inputs that lack the dim are unchanged (so an
  ensemble Zarr and a 1D obs Zarr can share the same `--along number`).
  `--along` traces are lines even when `--style bar`. `--trace` selectors
  refer to the `--input` (1-based index / label), not to individual members.
- `--title` — optional figure title. Titles longer than about 56 characters
  wrap onto a second line at a `·` / `:` / word break.
- `--xlabel` / `--ylabel` — optional axis-label overrides. When omitted, x is
  blank if the ticks are dates (`Time` / `Valid time` are redundant);
  otherwise `Calendar day` or the time dim.
  Y comes from the variable metadata. Passed text is used as-is.
- `--fontsize` — base font size for titles, axis labels, ticks, and legend
  (default 16). Raise on user request (e.g. `--fontsize 22`).
- `--figsize` — figure size in inches as `W,H` or `WxH` (e.g. `10,6`).
  When set, the PNG is that canvas at 150 dpi (the overlay legend stays
  inside it). Default `10×6`, cropped tightly.
- `--style` — `line` (default) or `bar`. Default for every series; a per-trace
  `style=line|bar` on `--trace` overrides it. `bar` draws grouped bars (one
  group per time step; one bar per bar-styled `--input`, offset within the
  group). Bar width is 80% of the median time spacing, split across bar
  series only (line overlays do not take a bar slot). Single-input `bar` is
  just one bar per time.
- `--subplots` — one stacked panel per `--input`, sharing the time axis, with
  an independent y-scale (and y-label) on each. Use this when the series have
  different units or ranges. Default is overlay on one axes. `--along` still
  fans members inside that input's panel. `--trace` still selects by `--input`.
- `--align-day-of-year` — opt-in (default off). Plot each trace against its
  day-of-year (1–366) instead of its absolute date, so inputs from different
  years overlay on a shared x-axis. Tick labels show calendar dates (e.g.
  `1 Oct`); the x-axis
  label is `calendar day`.
- `--band LOW,HIGH` — with `--along`, draw a filled percentile envelope (e.g.
  `--band 10,90`) plus the mean line instead of spaghetti members. Requires
  `--along`. Not valid on bar traces.
- `--theme` — `weather_skills` (seaborn `deep` colorway, default) or
  `colorblind`. Line colors follow seaborn unless `--trace` sets `color=`.
  Caveats:
  - Requires a calendar-date time axis. It errors (exit 2) on a non-date axis,
    such as a forecast `step` timedelta; drop the flag or select a date dim
    with `--time-dim`.
  - Intended for within-year seasons. A season that crosses the calendar-year
    boundary (e.g. Dec–Feb) wraps at the year boundary (day 366 → 1 in leap
    years, 365 → 1 otherwise) and will not align as one contiguous block; the
    skill prints a stderr warning and still renders.
  - The 1–366 range assumes a standard calendar; model calendars yield their
    own range (e.g. a `360_day` calendar yields 1–360), so overlaying inputs
    on different calendars can misalign by several days without error.
  - Leap vs non-leap years offset day-of-year by ~1 after Feb 29, so dates
    after February in a leap year land one day-of-year higher than the same
    date in a non-leap year.
- `--trace` — repeatable per-series style `SELECTOR:k=v[,k=v...]`. Unstyled
  series keep the matplotlib color cycle in `--input` order. Selectors:
  - `*` — all series (applied first; later `--trace` flags override)
  - a 1-based `--input` index (`4` is the fourth `-i`; only `1…N` count as
    indices, so `2026` is a year token, not input 2026)
  - the legend label (filename stem, or `station_id`)
  - a unique alphanumeric token in that label (`2026` matches `chirps_2026`)
  Keys: `color` (matplotlib name, hex, or grayscale `0-1`), `linewidth` /
  `lw`, `linestyle` / `ls`, `marker`, `markersize` / `ms`, `alpha`,
  `zorder`, `style` (`line` or `bar`; overrides global `--style` for that
  series). Line-only keys (`linewidth`, `linestyle`, `marker`, `markersize`)
  error on a bar series. Quote hex colors (`--trace '2026:color=#222'`).
  An unmatched or ambiguous selector exits 2.

### Output

A PNG at `--output`. Overlay mode is a single axes (default `figsize=(10, 6)`).
`--subplots` is one stacked panel per `--input` (taller default). Override with
`--figsize`. One series per `--input`
(line with markers, `--along` spaghetti, or bars; mixed `--trace style=` overlays a line on bars), legend below the traces. The y-axis label is the variable `long_name` (then
`GRIB_name`, then the variable name) plus `[<units>]` when the variable
carries a `units` attribute. Units are a short display form (`mm/day`,
`°C`), not the on-disk CF string.

### Input units

In overlay mode, all traces share one y-axis whose label takes the units of the
first input. When the overlaid inputs carry the plotted variable in differing
`units`, series in different units are drawn against a single scale and labeled
with only one of them. The skill prints a warning to stderr naming the distinct
units and still renders (exit status 0); pass `--subplots` for independent
y-axes. It is a rendering caveat, not a hard error.
Only inputs that carry a `units` attr participate in the comparison.

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata. Read-back:

```bash
python3 -c "from PIL import Image; import json; img = Image.open('out.png'); print(json.loads(img.info['weather_skills_history']))"
```

## Examples

Two series as stacked subplots (independent y-scales, shared time axis):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/chirps.zarr -i /tmp/imerg.zarr \
    --variable precip --reduce latitude --reduce longitude \
    --subplots --label CHIRPS --label IMERG \
    --output /tmp/precip_panels.png
```

Two forecast Zarrs, both already point-extracted (1D along `step`):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_nairobi.zarr -i /tmp/ifs_nairobi.zarr \
    --variable tp --output /tmp/forecasts.png \
    --title "Nairobi precip forecast"
```

Two gridded Zarrs averaged over space and ensemble:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_kenya.zarr -i /tmp/imerg_kenya.zarr \
    --variable tp \
    --reduce number --reduce latitude --reduce longitude \
    --output /tmp/precip_ts.png
```

101-member ensemble difference traces from one Zarr (plus a 1D overlay):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ens_diff.zarr -i /tmp/obs_diff.zarr \
    --variable tp \
    --reduce latitude --reduce longitude --along number \
    --trace '*:alpha=0.35,linewidth=0.8,marker=none' \
    --trace 'obs:color=black,linewidth=2,alpha=1,marker=o' \
    --output /tmp/ens_traces.png \
    --title "Ensemble difference traces"
```

Period totals as grouped bars (forecast vs observations):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_weekly.zarr -i /tmp/imerg_weekly.zarr \
    --variable tp --style bar \
    --reduce latitude --reduce longitude \
    --output /tmp/precip_bars.png \
    --title "Weekly precip totals"
```

Analog years in gray, current year emphasized (token match on the stem):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/chirps_2006.zarr -i /tmp/chirps_2015.zarr -i /tmp/chirps_2026.zarr \
    --align-day-of-year --reduce latitude --reduce longitude \
    --trace '*:color=0.65,linewidth=1.2' \
    --trace '2026:color=black,linewidth=2.5,zorder=5,markersize=7' \
    --output /tmp/analogs.png \
    --title "Kenya OND analog rainfall"
```

Observed period totals as bars, climatology as a heavy dashed line:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/obs.zarr -i /tmp/clim.zarr \
    --variable tp --style bar \
    --reduce latitude --reduce longitude \
    --trace 'clim:style=line,linestyle=--,linewidth=2.5,marker=none' \
    --output /tmp/obs_vs_clim.png \
    --title "30-day precip vs climatology"
```

Replot from the sidecar after editing title/style/reduce:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    --spec /tmp/precip_ts.plot.json \
    --output /tmp/precip_ts.png
```

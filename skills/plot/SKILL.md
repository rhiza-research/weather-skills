---
name: plot
description: Render a 2D heatmap, filled-contour map, 1D time series, xy scatter, wind-rose, u/v quiver, or layered map PNG from weather-skills standard dataset Zarrs. Name files with -i, --x/--y, or repeatable --layer KIND:PATH. Set every other parameter in --spec (kind, variable, title, colormap, bbox, fontsize). --dump-spec prints the merged spec. Heatmaps overlay coastlines, borders, lakes, and admin-1 boundaries. For precipitation, run aggregate-temporal then convert-to-totals first. For a lead-week verification grid, use plot-verify.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot

Source-agnostic visualization. Single-input kinds (`-i`) plus layered maps
(`--layer`, repeatable). Kind, variable, titles, colormap, bbox, and the
other knobs below are `--spec` keys, not command-line flags:
- `heatmap` — lon/lat heatmap (matplotlib `pcolormesh`) with scale-appropriate
  Natural Earth coastlines, country borders, filled lakes and (on country-scale
  views) admin-1 boundaries, equal geographic aspect, and a shared colorbar.
  Single-input map kinds are compiled as one-layer figures, so this and
  `--layer heatmap:<path>` produce the same picture.
  If the input has a `step` (or `time`) dimension, panels are laid out one per
  step with a shared color scale and a colorbar (right if one panel, bottom if
  several). Panel titles show calendar dates (`14 Sept '26`) or,
  for multi-day bins, inclusive ranges (`4–10 Aug '26`); forecast lead panels
  keep `<start> until <end>`. The colorbar label is the **variable** (and
  units), not those dates — `Total precipitation [mm]`, not `14 Sept '26`.
  Default layout is up to 4 columns (rows added as
  needed). `--rows` and/or `--columns` override that; leftover cells stay
  blank. Ensemble members (`number` dim) are averaged. Use `--index` to
  override the default reduction for any other extra dim. Precipitation totals
  default to a nested absolute-mm palette (same color = same millimetres;
  the colorbar window follows `aggregation_period`). When plotting rainfall
  anomalies, omit `--colormap` so the default diverging millimetre classes
  apply. Dump the resolved spec
  with `--dump-spec -` when you need to inspect the merged spec.
- `contour` — the same map layout as `heatmap` (panels, shared color scale,
  colorbar, geo overlays, `--bbox` / `--mask-geojson` / `--extent` /
  `--cities` / `--index` / `--draw-box` / `--rows` / `--columns`), compiled
  as filled contours (`contourf`) with thin black isolines.
  Values are interpolated between grid points rather than drawn as cell
  rectangles. Cannot mix with `--layer`.
- `timeseries` — 1D profile. Line plus a marker at each time point. Leftover
  non-time dims are **not** averaged for you: pass `--reduce` once per dim
  (`--reduce latitude --reduce longitude`) or `--along` to draw one line per
  value of that dim (`--along number`). A forecast cube (`step` lead times +
  scalar init `time`) is plotted against **valid time** (`init + step`) with
  calendar dates on the x-axis, not raw lead-time nanoseconds. An analysis /
  obs cube with a `time` dim is plotted against that axis as-is. For several
  series as stacked panels, use `plot-timeseries --subplots`.
- `xy` — scatter one 1D series against another. Pass `--x` and `--y` Zarrs
  (or one `-i` with `--x-variable` and `--y-variable`). Each input is reduced
  the same way as `timeseries` (mean over non-time dims; `--bbox` /
  `--mask-geojson` subset first when lat/lon remain). `--pair-on time`
  (default) inner-joins on the time/valid-time coord; `year` joins on
  calendar year (September IOD vs October rain); `index` pairs by position
  (same length required). Duplicate keys are an error — aggregate or select
  first. Points are labeled with the pair key when `--pair-on year`, or when
  `--pair-on time` and there are ≤ 25 points. Distinct from `--layer scatter`,
  which plots stations on a map.
- `windrose` — one polar rose of meteorological-from wind direction (0° = N,
  90° = E, clockwise) stacked by speed. Converts eastward (`u`) and northward
  (`v`) components; auto-detects `u10`/`v10`, CF `eastward_wind` /
  `northward_wind`, and other common pairs, or pass `--u-variable` /
  `--v-variable`. Flattens remaining space, time/step, and ensemble dims into
  samples (does **not** average the ensemble — a frequency rose needs the
  members). `--bbox` / `--mask-geojson` / `--index` subset samples first.
  16 compass sectors; speed classes 0–2, 2–4, …, ≥12 m/s (empty high-speed
  bins dropped).   `--colormap` colors the speed stacks (default blue→orange).
- `quiver` — wind-vector map: speed as `pcolormesh` (`YlGn` by default,
  matching `plot_s2s` 10 m / 700 hPa `10m-wind_vectors.png`) with native-grid
  `u`/`v` arrows like `plot_wind_and_sst_anomaly` (optional `--quiver-step`
  stride; no cartopy `regrid_shape`). Arrow length is auto-scaled so a
  typical wind is about 1.5× the subsampled grid spacing — a fixed matplotlib
  `scale=100` matches S2S *anomaly* magnitudes and overdraws 10 m/s basin
  winds. Same panel layout, geo overlays, `--bbox` / `--mask-geojson` /
  `--index` / `--cities` / `--draw-box` as heatmap. Ensemble `number` is
  averaged. Auto-detects u/v like windrose (`u10`/`v10`, CF `eastward_wind` /
  `northward_wind`, …). Colorbar is `Wind speed [m/s]` (or `Wind speed
  anomaly` if the u field name says so). Arrow keys for 5 and 10 m/s. Finer
  grids (GFS 0.25°) auto-thin to ~1.5° (native S2S spacing) unless
  `--quiver-step` is set.

Layered maps (`--layer KIND:PATH`, repeatable) draw several inputs on the
**same** axes. Each heatmap is drawn from that Zarr's own lat/lon; a 1.5°
forecast and a 0.05° CHIRPS field can share one map without `coarsen`.
There is no per-layer panel: `layers[].panel` is not a spec key, and
`--rows` / `--columns` only tile time or `step` slices of that one map.
`-i` is the single-input shorthand and cannot mix with `--layer`.
Kinds: `heatmap` (gridded Zarr), `scatter` (`station_id` / `point_id` Zarr),
`quiver` (gridded u/v arrows; speed mesh only if there is no heatmap layer),
`outline` (GeoJSON edges), `mask` (GeoJSON NaN mask, same as `--mask-geojson`).
Optional `::k=v` suffix: `variable`, `colormap`, `index`, `u-variable`,
`v-variable`, `quiver-scale`, `quiver-step`, `vmin`, `vmax`. Figure-level `--variable` /
`--colormap` / `--index` / `--vmin` / `--vmax` are defaults a layer inherits. A forecast `step` axis
still panels one map per lead; static layers (outline, cities, a single-time
field) repeat on every panel. Another data layer on the same axis kind is
intersected on labels. Overlaying calendar `time` on a raw `step` forecast is
an error — run `step-to-time` first. Same-variable heatmap+scatter layers share
one color scale unless `--independent-scale`.

## When to use

- Overlaying stations or a GeoJSON outline on a forecast/obs heatmap
  (`--layer heatmap:… --layer scatter:…`).
- Producing a quick-look forecast map panel for any gridded dataset.
- Producing a time/step profile for a gridded or station standard dataset.
- Scattering one index or field against another (IOD vs rainfall, two
  variables in one Zarr).
- Producing a wind rose from u/v (or eastward/northward) components.
- Producing S2S-style wind-vector maps (speed + quiver) from u/v.
- Precipitation: only after `aggregate-temporal` and `convert-to-totals`.
  Fetchers write rates; figures should show period totals (`mm`).
  When plotting rainfall anomalies, omit `--colormap` so the default
  diverging millimetre classes apply.

`--layer` stacks grids on one map. It cannot put each dataset in its own
panel, and `layers[].panel` is not a key. Do not `coarsen` datasets onto
one grid just to draw them. A shared lat/lon grid is only for `difference`
and `verify`. For one obs week versus week-4 through week-1 forecasts with a
hits row, use `plot-verify`. For rainy-season onset dates from
`indicator --detect first`, use `plot` (do not average `number` first).

## Usage

Parameters live in `--spec`. The skill builds a spec from the files you name, then deep-merges `--spec` onto it. `--dump-spec` writes that merge and skips the PNG.

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i <in.zarr> -o <out.png> \
    --spec '{"inputs":[{"variable":"tp"}],"traces":[{"kind":"heatmap"}],"title":"Week 1"}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -o <out.png> \
    --layer heatmap:<a.zarr> --layer scatter:<b.zarr> \
    --spec '{"title":"IMERG vs TAHMO","layers":[{"id":"a","variable":"precip"},{"id":"b","variable":"precip"}]}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --x <x.zarr> --y <y.zarr> -o <out.png> \
    --spec '{"traces":[{"kind":"xy","pair_on":"year"}]}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i <in.zarr> --dump-spec -
```

### Arguments

- `--input`, `-i` — Zarr input. Mutually exclusive with `--layer` and with `--x` / `--y`. Optional when `--spec` already lists input paths.
- `--x` / `--y` — X- and Y-axis Zarrs for an xy figure.
- `--layer` — repeatable `KIND:PATH` (`heatmap`, `scatter`, `quiver`, `outline`, `mask`). No `::k=v`. Put layer options in `--spec` `layers[]`, matched by id (`a`, `b`, …).
- `--output`, `-o` — PNG path.
- `--theme-file` — external palette file (JSON or TOML). Not a spec key. Named colormaps resolve against this file, then `~/.config/weather-skills/plot.toml`.
- `--spec` — JSON object or path, always deep-merged onto the spec built from the opened files. Your values win. `inputs[]` merges by id, `traces[]` by `input` (else id), `layers[]` by id (else index). An empty list does not wipe the figure. A `patch` key inside the object is rejected. Paths inside `--spec` are opened only when no dataset flag was passed.
- `--dump-spec` — write the merged spec as JSON and skip the PNG. `--output` is not required. Bare `--dump-spec` or `-` prints to stdout; a path writes a file.

### Parameters (`--spec`)

Unset `traces[0].kind` stays `heatmap`. Font size stays 16 when `theme.fontsize` is absent.

- `traces[0].kind` — `heatmap`, `contour`, `timeseries`, `xy`, `windrose`, `quiver`.
- `inputs[].variable`, `title`, `subplot_titles`, `xlabel`, `ylabel`, `cbar_label`, `legend`, `vmin`, `vmax`.
- `theme.colormap`, `theme.fontsize`, `theme.template` (`weather_skills` or `colorblind`), `theme.rc`.
- `geo.bbox` as `[N, W, S, E]`, `geo.extent`, `geo.mask_geojson`, `geo.cities`, `geo.draw_boxes`.
- `layout.figsize` as `[W, H]`, `layout.facet.rows` / `columns` / `wspace` / `hspace`, `layout.shared_colorscale`.
- `layers[]` — `variable`, `colormap`, `vmin`, `vmax`, `index`, `u_variable`, `v_variable`, `quiver_scale`, `quiver_step`.
- `traces[].reduce`, `along`, `pair_on`, `x_variable`, `y_variable`, `u_variable`, `v_variable`, `index`.

### Output

A PNG at `--output`. Stdout prints a pixel `plot hash` (sha256 of RGB
pixels) and `data: not null (<var> N/M finite)` or `data: NULL`. Compare
hashes across runs to see whether the figure changed. `NULL` means every
plotted variable is all-NaN — run `inspect-zarr` on the input. Look at
the PNG as well. `--dump-spec` skips the PNG and this report.

The colorbar (and timeseries y-axis) label resolves
from variable attrs: `long_name` → `GRIB_name` → bare variable name →
`"value"`, suffixed with `[units]` when the `units` attr is present. That
label is the field, not the time coordinate — dates stay on panel titles.
Units on the figure are a short display form (`mm/day`, `°C`, `mm`, `m/s`), not the
on-disk CF string. A wind rose labels speed stacks in those display units and
the radial axis as frequency percent. A quiver map colors speed and overlays
u/v arrows; the colorbar is `Wind speed [m/s]`.
Prefer an amount Zarr from
`convert-to-totals` (labeled `Total precipitation [mm]`). If the input is
still a precip **rate** with `aggregation_period`, plot converts it to a
period total for the figure only. Unaggregated fetch rates stay `mm day-1`.

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata (same schema as Zarr provenance). Read-back:

```bash
python3 -c "from PIL import Image; import json; print(json.loads(Image.open('out.png').info['weather_skills_history']))"
```

Or:

```bash
exiftool out.png
```

## Examples

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --spec '{"inputs":[{"variable":"tp"}],"title":"S2S precip"}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf_contour.png \
    --spec '{"inputs":[{"variable":"tp"}],"traces":[{"kind":"contour"}],"title":"S2S precip"}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/weekly.zarr -o /tmp/weekly.png \
    --spec '{"inputs":[{"variable":"tp"}],"layout":{"facet":{"rows":2,"columns":3,"wspace":0.25,"hspace":0.25}}}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -o /tmp/imerg_vs_tahmo.png \
    --layer heatmap:/tmp/imerg.zarr --layer scatter:/tmp/tahmo.zarr \
    --layer outline:/tmp/kenya.geojson \
    --spec '{"title":"IMERG vs TAHMO","layers":[{"id":"a","variable":"precip"},{"id":"b","variable":"precip"}]}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --x /tmp/iod_sep.zarr --y /tmp/rain_oct.zarr \
    -o /tmp/iod_vs_rain.png \
    --spec '{"traces":[{"kind":"xy","pair_on":"year"}],"xlabel":"September IOD","ylabel":"October rainfall"}'
```

## Inspect the merged spec

PNG is the only artifact. There is no `*.plot.json` sidecar and no `--patch`.

1. `plot -i data.zarr -o out.png --spec '{"title":"Precip","inputs":[{"variable":"precip"}]}'`
2. Re-run with `--dump-spec -` (no PNG) when you need to see the merged object. The full JSON is token-expensive.
3. Change `--spec` and run again. A dumped spec passed back as `--spec` merges onto the internal spec; file flags still choose the datasets when they are present.

## Matplotlib options in the spec

Values must be JSON (strings, numbers, bools, lists, objects). Unknown keys
on artist/axes objects are errors. There is no `eval` and no Python callables.

Put matplotlib `rcParams` in `theme.rc`. They apply after
the seaborn theme, so they win. Backend / interactive keys (`backend`,
`interactive`, `tk.*`, …) are rejected. Any other matplotlib rcParam is
accepted; an unknown name is an error. Do not invent
`theme.subplot_title_fontsize` / `theme.label_fontsize` — those keys are
rejected and name the `theme.rc.*` path.

`--fontsize` writes the seven size keys below. `--dump-spec` always
includes those resolved values. Patch any of them (or the weight / pad /
linewidth keys) without a dump:

```json
{"theme": {"rc": {"axes.titlesize": 10, "xtick.labelsize": 8, "lines.linewidth": 2}}}
```

| `theme.rc` key | What it changes |
| --- | --- |
| `axes.titlesize` | map panel titles (auto dates or `--subplot-title`) |
| `figure.titlesize` | figure `--title` |
| `axes.labelsize` | x/y axis labels **and** the colorbar label. Colorbar-only size is `layout.colorbar.labelsize` |
| `xtick.labelsize` / `ytick.labelsize` | tick labels |
| `legend.fontsize` / `legend.title_fontsize` | legend text |
| `font.size` | fallback size when a more specific key is unset |
| `font.family` / `font.weight` | typeface and default weight |
| `axes.titleweight` / `figure.titleweight` | panel / figure title weight (`bold`) |
| `axes.titlepad` / `axes.labelpad` | gap from a **panel** title or **every** axis label (lon/lat **and** colorbar) to the axes. Colorbar-only pad / size are `layout.colorbar.labelpad` / `labelsize`. Figure-title height is `layout.suptitle.y`, not `axes.titlepad` |
| `xtick.major.pad` / `ytick.major.pad` | gap from tick labels to the spines |
| `axes.labelweight` | axis-label weight |
| `lines.linewidth` | default line width (timeseries / xy) |
| `axes.linewidth` | spine thickness |

Use `layout.dpi` / `layout.figsize` / `layout.facecolor`, not `figure.dpi`
/ `figure.figsize` / `figure.facecolor`. A single series' width belongs on
`traces[].line.linewidth`, not `lines.linewidth`.

| Spec key | Matplotlib surface |
| --- | --- |
| `axes` | Matplotlib Axes config applied after the data are drawn: scales, limits, labels, **ticks** (`xticks`/`yticks` lists or `{values, labels}`), locators, formatters, spines, grid, legend, twins. `xlabel` / `ylabel` may be a string or `{text, loc, pad, coords, rotation, ha, va, …}` (`coords` is `[x, y]` in axes fraction; omit `text` to keep the already-drawn label). Same object on every figure skill. A dump includes only the keys you set. |
| `annotations` | `ax.text` or `ax.annotate` (`xy`, `xytext`, `arrowprops`, fonts, `bbox`). `xref: paper` / `transform: axes` uses axes fraction. `axes`/`panel` picks a subplot |
| `shapes` | `rect`, `hline`, `vline`, `hspan`, `vspan`, `line`, `circle`/`ellipse` |
| `traces[].line` / `.mesh` / `.contour` / `.scatter` / `.bar` / `.quiver` / `.windrose` | kwargs for the matching artist (`linewidth`, `alpha`, `marker`, `shading`, `levels`, `scale`, `nsector`, …). `contour.lines: false` skips isoline overlay |
| `traces[].fill` | `fill_between` for `--band` |
| `traces[].mediogram` | `{width, forecast, mclimate, mean, legend}` for box colors / mean line |
| `layout.colorbar` | see the table below. set these in `--spec`; unknown keys error |
| `layout.facet.wspace` / `hspace` | inter-panel gap as a fraction of panel size (`--panel-spacing`) |
| `layout.facecolor`, `layout.dpi` | figure patch and DPI |
| `theme.rc` | matplotlib rcParams after seaborn (see the table above). `--dump-spec` always includes the `--fontsize` sizes |

`layout.colorbar` via `--spec '{"layout": {"colorbar": {…}}}'`. An unknown key is an error:

| `layout.colorbar` key | What it changes | CLI |
| --- | --- | --- |
| `labelpad` | points between colorbar ticks and the **colorbar label** (not lon/lat) | `--spec` |
| `labelsize` | colorbar label font size (not lon/lat, not colorbar ticks) | `--spec` |
| `ticksize` | colorbar tick-label font size (not map lon/lat ticks) | `--spec` |
| `pad` | gap between the map axes and the colorbar **strip** | — |
| `len` / `shrink` | colorbar length as a fraction of the axes | — |
| `thickness` | colorbar thickness in points (`> 1`) or a fraction (`≤ 1`) | — |
| `location` / `orientation` | `right`, `bottom`, … | — |
| `extend` / `extendfrac` / `extendrect` | arrows past the ends of the scale | — |
| `ticks` / `labels` | tick positions and text (same count) | `--cbar-ticks` / `--cbar-labels` |
| `drawedges` / `spacing` / `format` | class edges, uniform/proportional spacing, tick format | — |

Every knob has exactly one home, and an unknown key is an error naming the
canonical path, so an edit never silently does nothing. Artist kwargs live on
the trace that draws them rather than at the top level.

A timeseries series can plot on a twin y-axis with `"twin": "y"`.
Every kind (heatmap, contour, timeseries, xy, windrose, quiver, `--layer`)
dumps the same `axes` / `annotations` / `shapes` / `theme.rc` objects and
applies them after the data are drawn. Reposition a polar windrose frequency
label with `--spec '{"axes": {"ylabel": {"coords": [1.15, 0.5], "rotation": 0}}}'`.
The full key table is in [`docs/plotting.md`](../../docs/plotting.md).

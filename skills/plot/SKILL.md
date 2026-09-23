---
name: plot
description: Render a 2D heatmap, filled-contour map, 1D time series, xy scatter, wind-rose, u/v quiver, or layered map PNG from weather-skills standard dataset Zarrs. Side-by-side maps on different grids (CHIRPS 0.05° next to ECMWF 1.5°) are one repeated -i per file: each file is its own heatmap panel on its own lat/lon. Do not coarsen them onto one grid. --layer stacks inputs on a single map and does not make a panel per dataset. Name files with repeatable -i, --x/--y, or repeatable --layer KIND:PATH. Set every other parameter in --spec. For precipitation, run aggregate-temporal then convert-to-totals first. For a lead-week verification grid, use plot-verify.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot

Name the files on the command line. Put every drawing choice in `--spec`.

Kind, variable, titles, colormap, map window, panel layout, index, reduce, and font size are spec keys: `traces[0].kind`, `inputs[0].variable`, `title`, `theme.colormap`, `geo.bbox`, `layout.facet.rows` / `columns`, `inputs[0].index`, `traces[0].reduce`, `theme.fontsize`. `--layer` is `KIND:PATH` only; options for that layer go on the matching `layers[]` entry. A `patch` key inside the JSON is rejected.

## Before guessing a flag or a key

There is no `--rows`, `--title`, `--fontsize`, `--cbar-label`, `--patch`, or any other per-knob flag — only the ones in **Command line** below. Every drawing choice is a JSON key under `--spec`, and an unknown or misplaced key is a hard error listing every valid key at that level (never a silent no-op). If you don't already know the shape of `--spec`, do **not** discover it by submitting guesses one at a time:

1. Run `--dump-spec -` with just `-i <one of your files>` and no `--spec` at all. It prints the complete, already-resolved default spec — every top-level section (`inputs`, `traces`, `layout`, `theme`, `geo`, `axes`, `annotations`, ...) with its real keys, so you can see the whole schema in one call instead of one rejected key at a time.
2. For the full key reference in prose, read the **Spec keys** table below, or [`docs/plotting.md`](../../docs/plotting.md).
3. Write one `--spec`, render once, and look at the resulting PNG (or run `inspect-figure` on it) before trying another variation. The `plot hash` printed after a render tells you only that the image changed, not what changed or how it looks — never use hash comparisons to choose between layout options (a title's position, panel spacing, colorbar placement). Look at the pixels.

## Side by side, or one map

| Goal | How |
| --- | --- |
| Two datasets in one PNG, each on its own lat/lon (0.05° beside 1.5°) | Pass `-i` once per file. Put a `subplots` entry on each cell. `row` and `col` are 1-based. `layers` on that cell stack. The cell's `title`, `vmin`, `vmax`, `colormap`, and `cbar_label` style that cell; a key on the layer wins. |
| Those same datasets drawn on top of each other | `--layer`. One axes. This does not make a panel per dataset. `layers[].panel` is not a key. |
| Several times or forecast steps of one dataset | One heatmap trace. `layout.facet.rows` and `columns` tile those slices. |

Different spacing is expected. Do not `coarsen` or `downscale` just to draw the figure. A shared lat/lon grid is only for `difference` and `verify`, which subtract cell by cell.

Each side-by-side trace has to already be one map. A `time` or `step` longer than one value is an error — aggregate it first (`aggregate-temporal`, then `convert-to-totals` for precipitation).

## Command line

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

- `-i`, `--input` — repeatable Zarr. One file is one heatmap, contour, or quiver panel. `timeseries` and `windrose` take a single `-i`. Mutually exclusive with `--layer` and with `--x` / `--y`. Optional when `--spec` already lists paths and no dataset flag was passed.
- `--x` / `--y` — the two Zarrs for `traces[0].kind` `xy`.
- `--layer` — repeatable `KIND:PATH` only (`heatmap`, `scatter`, `quiver`, `outline`, `mask`). Layer ids are `a`, `b`, `c`, … in this order. Options go on `layers[]` in `--spec`, matched by that id.
- `-o`, `--output` — PNG path. Required unless `--dump-spec` is set.
- `--spec` — JSON object or path. Deep-merged onto the spec built from the files you named. Your values win. `inputs[]` merges by `id`, `traces[]` by `input` (else `id`), `layers[]` by `id` (else index). An empty list does not wipe the figure.
- `--dump-spec` — write the merged spec as JSON and skip the PNG. Bare `--dump-spec` or `-` prints to stdout; a path writes a file. Run it with just `-i` and no `--spec` to see the complete default schema before writing one — that is the fastest way to learn what keys exist, faster than guessing a flag and reading the rejection. The full JSON is token-expensive once your `--spec` is large; at that point use it to check one key, then edit `--spec` and draw again.
- `--theme-file` — palette file (JSON or TOML). Not a spec key. Named colormaps resolve against this file, then `~/.config/weather-skills/plot.toml`.

Unset `traces[0].kind` stays `heatmap`. Unset `theme.fontsize` stays 16.

## Kinds

Set `traces[0].kind` in `--spec`.

- `heatmap` — lon/lat `pcolormesh` with coastlines, country borders, filled lakes, and (on country-scale views) admin-1 boundaries. One `-i` is one panel per `step` or `time`. Each extra `-i` adds a panel on that file's own lat/lon; see **Side by side, or one map**. Shared color scale, colorbar on the right for one panel and on the bottom for several. A single file's panel titles are calendar dates (`14 Sept '26`) or inclusive ranges (`4–10 Aug '26`); forecast leads keep `<start> until <end>`. Several files use `subplot_titles`, then `inputs[].label`, then the file name. The colorbar label is the variable and units (`Total precipitation [mm]`), not the date. Default grid is up to 4 columns. Set `layout.facet.rows` and `layout.facet.columns` to override; leftover cells stay blank. Ensemble `number` is averaged. `inputs[0].index` overrides the reduction for any other extra dim. Precipitation totals use a nested absolute-mm palette (same color = same millimetres; the window follows `aggregation_period`). For rainfall anomalies, omit `theme.colormap` so the diverging millimetre classes apply. A single-input heatmap and `--layer heatmap:<path>` draw the same picture.
- `contour` — the same map as `heatmap`, drawn with `contourf` and thin black isolines. Values are interpolated between grid points. Cannot be combined with `--layer`.
- `timeseries` — one line plus a marker at each time. Leftover non-time dims are not averaged: set `traces[0].reduce` to a list of dim names, or `traces[0].along` to draw one line per value of that dim. A forecast (`step` plus a scalar init `time`) is plotted against valid time (`init + step`). An analysis or obs cube is plotted against its `time` axis. For several series as stacked panels, use `plot-timeseries`.
- `xy` — scatter one 1D series against another. Pass `--x` and `--y`, or one `-i` with `traces[0].x_variable` and `traces[0].y_variable`. Each series is reduced like `timeseries` (`geo.bbox` / `geo.mask_geojson` subset first when lat/lon remain). `traces[0].pair_on` is `time` (default, inner-join on time or valid time), `year` (calendar year), or `index` (position; lengths must match). Duplicate keys are an error — aggregate or select first. Points are labeled when `pair_on` is `year`, or when it is `time` and there are 25 points or fewer. This is not `--layer scatter`, which draws stations on a map.
- `windrose` — one polar rose of meteorological-from direction (0° = N, 90° = E, clockwise), stacked by speed. Converts eastward `u` and northward `v`. Auto-detects `u10`/`v10` and CF `eastward_wind` / `northward_wind`, or set `traces[0].u_variable` and `traces[0].v_variable`. Remaining space, time, and ensemble dims become samples; the ensemble is not averaged. `geo.bbox`, `geo.mask_geojson`, and `inputs[0].index` subset samples first. 16 sectors; speed classes 0–2, 2–4, …, ≥12 m/s, with empty high bins dropped. `theme.colormap` colors the stacks (default blue→orange).
- `quiver` — wind-speed `pcolormesh` (default `YlGn`) with `u`/`v` arrows on the native grid. Arrow length is auto-scaled so a typical wind is about 1.5× the subsampled spacing. Set `traces[0].quiver.step` to stride and `traces[0].quiver.scale` to override arrow length. A `--layer quiver:` entry uses the same keys on `layers[].quiver`. Same panels and geo overlays as `heatmap`. Ensemble `number` is averaged. Finer grids auto-thin to about 1.5° unless `quiver.step` is set. Colorbar is `Wind speed [m/s]` (or `Wind speed anomaly` when the u field name says so), with arrow keys at 5 and 10 m/s. With `--layer`, use `--layer quiver:PATH` instead of kind `quiver`.

## Layers

`--layer` draws several inputs on the **same** axes. It is not the side-by-side layout above. There is no per-layer panel: `layers[].panel` is not a key.

`heatmap`, `scatter` (`station_id` / `point_id`), and `quiver` read Zarrs. `outline` draws GeoJSON edges. `mask` is a GeoJSON NaN mask, the same idea as `geo.mask_geojson`. A layer inherits `inputs[].variable`, `theme.colormap`, `inputs[].index`, `vmin`, and `vmax` when its own `layers[]` entry omits them. A forecast `step` axis still panels one map per lead; a static layer (outline, cities, a single-time field) repeats on every panel. Another data layer on the same axis kind is intersected on labels. Overlaying calendar `time` on a raw `step` forecast is an error — run `step-to-time` first. Same-variable heatmap and scatter layers stacked with `--layer` share one color scale unless `layout.shared_colorscale` is `false`. Side-by-side traces and `subplots[]` cells each scale independently by default, even with the same variable — set `layout.shared_colorscale: true` to share one colorbar across all of them.

`--layer` cannot be combined with kind `timeseries`, `xy`, `windrose`, or `contour`.

## When to use

- CHIRPS next to a forecast, each at its own resolution, in one PNG. Two heatmap traces. Do not coarsen the forecast onto the obs grid for this.
- Stations or a GeoJSON outline on a forecast or obs heatmap. Use `--layer` for that overlay.
- A quick-look map or a time/step profile.
- One index against another (IOD vs rainfall, or two variables in one Zarr).
- A wind rose or an S2S-style wind-vector map from u/v.
- Precipitation only after `aggregate-temporal` and `convert-to-totals`. Fetchers write rates; the figure should show period totals (`mm`). For rainfall anomalies, omit `theme.colormap`.

Do not `coarsen` datasets onto one grid just to draw them. A shared lat/lon grid is for `difference` and `verify`. For one obs week versus week-4 through week-1 forecasts with a hits row, use `plot-verify`. For rainy-season onset dates from `indicator --detect first`, use `plot` and do not average `number` first.

## Examples

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --spec '{"inputs":[{"variable":"tp"}],"title":"S2S precip"}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf_contour.png \
    --spec '{"inputs":[{"variable":"tp"}],"traces":[{"kind":"contour"}],"title":"S2S precip"}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/weekly.zarr -o /tmp/weekly.png \
    --spec '{"inputs":[{"variable":"tp"}],"layout":{"facet":{"rows":2,"columns":3,"wspace":0.25,"hspace":0.25}}}'

# Side by side. Each -i is an input (a, then b). Each subplot is a cell.
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py \
    -i /tmp/chirps.zarr -i /tmp/ecmwf.zarr -o /tmp/chirps_vs_ecmwf.png --spec '{
  "inputs": [{"id": "a", "variable": "precip"}, {"id": "b", "variable": "tp"}],
  "subplots": [
    {
      "row": 1, "col": 1, "title": "CHIRPS 0.05°",
      "vmin": 0, "vmax": 50, "colormap": "Blues", "cbar_label": "Obs [mm]",
      "layers": [{"kind": "heatmap", "input": "a"}]
    },
    {
      "row": 1, "col": 2, "title": "ECMWF 1.5°",
      "vmin": 0, "vmax": 200, "colormap": "YlGn", "cbar_label": "Forecast [mm]",
      "layers": [{"kind": "heatmap", "input": "b"}]
    }
  ],
  "geo": {"bbox": [11.3, -3.5, 4.5, 1.3]}
}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -o /tmp/imerg_vs_tahmo.png \
    --layer heatmap:/tmp/imerg.zarr --layer scatter:/tmp/tahmo.zarr \
    --layer outline:/tmp/kenya.geojson \
    --spec '{"title":"IMERG vs TAHMO","layers":[{"id":"a","variable":"precip"},{"id":"b","variable":"precip"}]}'

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --x /tmp/iod_sep.zarr --y /tmp/rain_oct.zarr \
    -o /tmp/iod_vs_rain.png \
    --spec '{"traces":[{"kind":"xy","pair_on":"year"}],"xlabel":"September IOD","ylabel":"October rainfall"}'
```

## Output

A PNG at `--output`. Stdout prints a pixel `plot hash` (sha256 of RGB pixels) and `data: not null (<var> N/M finite)` or `data: NULL`. `NULL` means every plotted variable is all-NaN — run `inspect-zarr` on the input. A changed hash only proves the pixels differ, not what changed or whether it looks right (a title moved above vs. inside a panel hashes just as differently as a broken render) — comparing hashes across a few candidate specs is never a substitute for looking at the PNG. Always look at the PNG, or run `inspect-figure` on it. `--dump-spec` skips the PNG and this report.

The colorbar and timeseries y-axis label come from variable attrs: `long_name`, then `GRIB_name`, then the variable name, then `"value"`, with `[units]` when present. Dates stay on panel titles. Display units are short (`mm/day`, `°C`, `mm`, `m/s`). A wind rose labels speed stacks in those units and the radial axis as frequency percent. Prefer an amount Zarr from `convert-to-totals` (`Total precipitation [mm]`). A precip rate that already has `aggregation_period` is converted to a period total for the figure only. Unaggregated fetch rates stay `mm day-1`.

Provenance is one `weather_skills_history` JSON array in the PNG metadata:

```bash
python3 -c "from PIL import Image; import json; print(json.loads(Image.open('out.png').info['weather_skills_history']))"
```

## Spec keys

Values are JSON. Unknown keys on artist or axes objects are errors. There is no `eval` and no Python callable.

| What you want | `--spec` path |
| --- | --- |
| Kind | `traces[0].kind` |
| Variable | `inputs[0].variable` (or `layers[].variable`) |
| Titles and axis text | `title`, `subplot_titles`, `xlabel`, `ylabel`, `cbar_label`, `legend`. `layout.facet.titles` and `traces[].title` are panel titles and are stored on `subplot_titles`. |
| Color limits | Figure `vmin`, `vmax` for every panel. One panel: `inputs[].vmin`, `inputs[].vmax`, `inputs[].colormap`, `inputs[].cbar_label`. `traces[].kind` and `traces[].mesh` / `contour` / `quiver` stay on that trace. |
| Colormap and font | `theme.colormap`, `theme.fontsize`, `theme.template` (`weather_skills` or `colorblind`), `theme.rc` |
| Map window | `geo.bbox` as `[N, W, S, E]`, `geo.extent`, `geo.mask_geojson`, `geo.cities`, `geo.draw_boxes` |
| Panels | `layout.figsize` as `[W, H]`, `layout.dpi`, `layout.facecolor`, `layout.facet.rows` / `columns` / `wspace` / `hspace`. One heatmap trace: rows and columns tile `time` or `step`. Several heatmap traces: one panel per trace. |
| Shared color scale | `layout.shared_colorscale` (`false` = never auto-share, even stacked `--layer` entries; `true` = one colorbar across every panel/cell), plus figure-level `vmin` / `vmax`. Unset: `--layer` stacks auto-share same-variable layers; side-by-side traces and `subplots[]` cells scale independently |
| Extra-dim reduction | `inputs[0].index`, `traces[0].reduce`, `traces[0].along` |
| xy / wind | `traces[0].pair_on`, `x_variable`, `y_variable`, `u_variable`, `v_variable`, `quiver.step`, `quiver.scale` |
| One layer's options | `layers[]` entry with that layer's `id` |

`theme.rc` applies after the seaborn theme, so it wins. Backend and interactive keys (`backend`, `interactive`, `tk.*`, …) are rejected. Do not invent `theme.subplot_title_fontsize` or `theme.label_fontsize`. `theme.fontsize` fills the seven size keys below; `--dump-spec` includes the resolved values.

| `theme.rc` key | What it changes |
| --- | --- |
| `axes.titlesize` | map panel titles |
| `figure.titlesize` | figure title |
| `axes.labelsize` | x/y labels and the colorbar label. Colorbar-only size is `layout.colorbar.labelsize` |
| `xtick.labelsize` / `ytick.labelsize` | tick labels |
| `legend.fontsize` / `legend.title_fontsize` | legend text |
| `font.size` | fallback when a more specific key is unset |
| `font.family` / `font.weight` | typeface and default weight |
| `axes.titleweight` / `figure.titleweight` | panel / figure title weight |
| `axes.titlepad` / `axes.labelpad` | gap from a panel title or every axis label to the axes. Colorbar-only pad is `layout.colorbar.labelpad`. Figure-title height is `layout.suptitle.y` |
| `xtick.major.pad` / `ytick.major.pad` | gap from tick labels to the spines |
| `axes.labelweight` | axis-label weight |
| `lines.linewidth` | default line width. One series belongs on `traces[].line.linewidth` |
| `axes.linewidth` | spine thickness |

Use `layout.dpi`, `layout.figsize`, and `layout.facecolor`, not `figure.dpi`, `figure.figsize`, or `figure.facecolor`.

| Spec key | Matplotlib surface |
| --- | --- |
| `axes` | Applied after the data are drawn: scales, limits, labels, ticks (`xticks` / `yticks` as lists or `{values, labels}`), locators, formatters, spines, grid, legend, twins. `xlabel` / `ylabel` may be a string or `{text, loc, pad, coords, rotation, ha, va, …}` (`coords` is `[x, y]` in axes fraction; omit `text` to keep the drawn label). A dump includes only the keys you set. |
| `annotations` | `ax.text` or `ax.annotate`. `xref: paper` / `transform: axes` uses axes fraction. `axes` / `panel` picks a subplot. |
| `shapes` | `rect`, `hline`, `vline`, `hspan`, `vspan`, `line`, `circle` / `ellipse` |
| `traces[].line` / `.mesh` / `.contour` / `.scatter` / `.bar` / `.quiver` / `.windrose` | kwargs for that artist. `contour.lines: false` skips the isoline overlay |
| `traces[].fill` | `fill_between` for a band set on `traces[0].band` |
| `traces[].mediogram` | `{width, forecast, mclimate, mean, legend}` |
| `layout.facet.wspace` / `hspace` | gap between panels, as a fraction of panel size |
| `layout.colorbar` | see below. Unknown keys error |

| `layout.colorbar` key | What it changes |
| --- | --- |
| `labelpad` | points between colorbar ticks and the colorbar label |
| `labelsize` | colorbar label font size |
| `ticksize` | colorbar tick-label font size |
| `pad` | gap between the map axes and the colorbar strip |
| `len` / `shrink` | colorbar length as a fraction of the axes |
| `thickness` | thickness in points (`> 1`) or a fraction (`≤ 1`) |
| `location` / `orientation` | `right`, `bottom`, … |
| `extend` / `extendfrac` / `extendrect` | arrows past the ends of the scale |
| `ticks` / `labels` | tick positions and text (same count) |
| `drawedges` / `spacing` / `format` | class edges, uniform or proportional spacing, tick format |

A timeseries series can use a twin y-axis with `"twin": "y"`. Reposition a wind-rose frequency label with `--spec '{"axes": {"ylabel": {"coords": [1.15, 0.5], "rotation": 0}}}'`. The full key table is in [`docs/plotting.md`](../../docs/plotting.md).

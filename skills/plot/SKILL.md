---
name: plot
description: Render a 2D heatmap, filled-contour map, 1D time series, xy scatter, wind-rose, u/v quiver, or layered map PNG from weather-skills standard dataset Zarrs. Overlay multiple inputs with repeatable --layer KIND:PATH (heatmap, scatter, quiver, GeoJSON outline/mask). Heatmaps overlay scale-appropriate coastlines, country borders, lakes, and admin-1 boundaries. Use for a single dataset as a map/profile/rose/vectors, or stacked layers (e.g. precip heatmap + station scatter). --kind xy plots one 1D series against another (--x/--y, or -i with --x-variable/--y-variable), pairing on time, year, or index. Override figure text with --title, --subplot-title (repeatable panel titles), --xlabel/--ylabel, and --cbar-label. For precipitation, run aggregate-temporal then convert-to-totals first. For side-by-side two-row comparison, use plot-compare. Use --fontsize to enlarge titles, axis labels, city labels, and colorbar text (default 16).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot

Source-agnostic visualization. Single-input kinds (`-i`) plus layered maps
(`--layer`, repeatable):
- `heatmap` — lon/lat heatmap (matplotlib `pcolormesh`) with scale-appropriate
  Natural Earth coastlines, country borders, filled lakes and (on country-scale
  views) admin-1 boundaries, equal geographic aspect, and a shared colorbar.
  Single-input map kinds are compiled as one-layer figures, so this and
  `--layer heatmap:<path>` produce the same picture.
  If the input has a `step` (or `time`) dimension, panels are laid out one per
  step with a shared color scale and a colorbar (right if one panel, bottom if
  several). Panel titles show calendar dates (`14 Sept '26`) or,
  for multi-day bins, inclusive ranges (`4–10 Aug '26`); forecast lead panels
  keep `<start> until <end>`. Default layout is up to 4 columns (rows added as
  needed). `--rows` and/or `--columns` override that; leftover cells stay
  blank. Ensemble members (`number` dim) are averaged. Use `--index` to
  override the default reduction for any other extra dim. Precipitation totals
  default to a nested absolute-mm palette (same color = same millimetres;
  the colorbar window follows `aggregation_period`). Dump the resolved spec
  with `--dump-spec -` only when you need to inspect knobs, then `--patch`.
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
**same** axes. `-i` is the single-input shorthand and cannot mix with `--layer`.
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
- If the PNG looks empty or wrong, run `inspect-figure` on it (then
  `inspect-zarr` on the input Zarr) before regenerating.

For two-dataset **side-by-side** (two-row) comparison, use `plot-compare`.
To overlay stations on a heatmap, use `--layer` here instead. For N gridded
datasets as a valid-time grid with blank cells where a dataset has no time,
use `plot-compare-forecasts`. For one obs week versus week-4 through week-1 forecasts
with a hits row, use `plot-verify`. For rainy-season onset dates from
`indicator --detect first`, use `plot` (do not average `number` first).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --input <in.zarr> --output <out.png> \
    [--variable NAME] [--kind heatmap|contour|timeseries|xy|windrose|quiver] \
    [--u-variable NAME] [--v-variable NAME] [--quiver-scale N] [--quiver-step N] \
    [--colormap NAME|COLORS|{JSON}] [--colormap-bounds 0,10,50] \
    [--colormap-under COLOR] [--colormap-over COLOR] \
    [--vmin N] [--vmax N] [--title TEXT] [--subplot-title TEXT ...] \
    [--xlabel TEXT] [--ylabel TEXT] [--cbar-label TEXT] \
    [--cbar-ticks 0,50,100] [--cbar-labels dry,mid,wet] \
    [--index DIM=POS,...] [--reduce DIM ...] [--along DIM] \
    [--extent LON_MIN,LON_MAX,LAT_MIN,LAT_MAX] \
    [--cities JSON_OR_PATH] [--fontsize N] [--figsize W,H] [--legend LOC] \
    [--bbox N/W/S/E] \
    [--mask-geojson PATH] [--draw-box N/W/S/E ...] \
    [--rows N] [--columns N] \
    [--spec PATH_OR_JSON] [--patch PATH_OR_JSON] [--dump-spec -|PATH] \
    [--theme-file PATH] [--theme weather_skills|colorblind]

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --output <out.png> \
    --layer heatmap:<a.zarr>[::variable=NAME] \
    [--layer scatter:<b.zarr>] [--layer outline:<c.geojson>] \
    [--layer quiver:<wind.zarr>] [--shared-scale | --independent-scale]

uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --kind xy --output <out.png> \
    --x <x.zarr> --y <y.zarr> [--x-variable NAME] [--y-variable NAME] \
    [--pair-on time|year|index] [--bbox N/W/S/E]
```

### Arguments
- `--input`, `-i` — Zarr input (single-dataset mode). Mutually exclusive with `--layer`
  and with `--x` / `--y`. Optional when `--spec` already lists input paths.
- `--spec` — optional full plot spec JSON (file or inline). A first run can
  be CLI flags only (`--title`, `--variable`, `--mask-geojson`, `--figsize`,
  …). Prefer `--patch` for edits. Pass `--spec` only when replaying a dumped
  object. Spec input paths are opened as Datasets so provenance still chains
  from the Zarr.
- `--patch` — optional JSON (file or inline) deep-merged onto this run's spec
  (CLI-built, or `--spec` if you passed one) before CLI flags overlay. Same
  knobs as `--spec` (`title`, `axes`, `layout`, `annotations`, `shapes`,
  `theme`, …). A `patch` key *inside* a spec object is rejected. Colorbar size:
  `{"layout": {"colorbar": {"len": 0.45, "thickness": 12}}}`.
  Reposition a polar windrose frequency label with
  `{"axes": {"ylabel": {"coords": [1.15, 0.5], "rotation": 0}}}`.
- `--dump-spec` — dump the resolved plot spec. Default: skip (PNG only).
  `-` prints JSON to stdout when you need to inspect knobs before `--patch`.
  A path writes a file. Token-expensive; omit unless `--patch` needs a key
  you cannot name from the CLI.
- `--theme` — `weather_skills` (seaborn `deep` colorway, default) or
  `colorblind`. Heatmap classified precip palettes are unchanged.
  Writes `theme.template` in a dumped spec.
- `--theme-file` — user palette registry (JSON or TOML). Unknown keys are an
  error. Named `--colormap` values resolve against this file, then
  `~/.config/weather-skills/plot.toml`.
- `--x` / `--y` — X- and Y-axis Zarrs for `--kind xy`. Mutually exclusive
  with `-i` and `--layer`.
- `--x-variable` / `--y-variable` — variables for `--kind xy`. Default: first
  data variable of `--x` / `--y` (or of `-i` when both flags are set).
- `--pair-on` — `time` (default), `year`, or `index`. `--kind xy` only.
- `--layer` — repeatable map layer `KIND:PATH` or `KIND:PATH::k=v`. Kinds:
  `heatmap`, `scatter`, `quiver`, `outline`, `mask`. Cannot mix with `-i` or
  with `--kind timeseries|xy|contour|windrose|quiver`.
- `--label` — colorbar label for each `--layer`, in order. When omitted,
  heatmap/scatter/quiver layers infer a short product name from provenance
  (or `--cbar-label` if that is set); outline/mask layers ignore it.
- `--shared-scale` / `--independent-scale` — layered heatmap/scatter color
  scales. Default: share when the layers resolve to the same variable and
  matching units.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable.
  Ignored for `--kind xy` (use `--x-variable` / `--y-variable`) and for
  `--kind windrose` and `--kind quiver` (use `--u-variable` / `--v-variable`).
- `--kind` — `heatmap` (default), `contour`, `timeseries`, `xy`, `windrose`, or
  `quiver`. `contour` is the heatmap layout with filled isolines instead of
  grid cells. Timeseries of a forecast (`step` + scalar init) uses valid times
  on the x-axis. `xy` is a 1D-vs-1D scatter (`--x`/`--y` or one `-i`); see
  `--pair-on`. Windrose converts u/v to meteorological-from direction (the
  direction the wind blows **from**) and speed, then histograms every remaining
  sample. Quiver is the S2S wind-vector map (speed field + arrows).
- `--u-variable` / `--v-variable` — eastward and northward wind variables for
  `--kind windrose` and `--kind quiver`. When omitted, the skill auto-detects
  a pair from CF `standard_name` (`eastward_wind` / `northward_wind`) or common
  names (`u10`/`v10`, `10m_u_component_of_wind`/`10m_v_component_of_wind`,
  `u`/`v`, …). Passing only one infers its partner (`u10` → `v10`).
  Heatmap/timeseries ignore these with a stderr warning.
- `--colormap` — matplotlib colormap name, comma-separated colors to
  interpolate (`white,wheat,green`), or a JSON object
  `{name, colors, bounds, under, over}` for a discrete class scale. Named
  matplotlib colormaps cannot contain commas, so a comma selects the custom
  list. When omitted, precipitation totals (rate or amount) use a nested
  absolute-mm palette (CHC hues: green→blue→purple→yellow→orange→red).
  The same millimetre class is always the same color; the colorbar is cropped
  by `aggregation_period`: 0–50 mm below 2 days (`ppt_daily`), 0–200 mm for
  2–9 days or a missing period (`ppt_week`), 0–400 mm for 10–39 days
  (`ppt_month`), 0–1000 mm for ≥40 days (`ppt_season`). Values above the
  window use the next master class (the same colour as that millimetre band
  on a wider window). The historical CHC rainbow (`ppt_total` /
  `chirps_total`, 0–2500 mm; `ppt_short` / `chirps_short`) remains as a
  named opt-in.
  Precipitation anomalies (negatives, or `anomal` in the name — e.g. after
  `difference`) use CHC `ppt_anomaly_cmap`
  (`[-500, -300, -200, -100, -50, -25, -10, 10, 25, 50, 100, 200, 300, 500]`
  mm with under/over colors). Percent-of-normal (`poa` / `%`) uses `ppt_poa`;
  SPI uses `spi`. Named aliases: `ppt_daily`, `ppt_week`, `ppt_month`,
  `ppt_season`, `ppt_total`/`chirps_total`, `ppt_short`/`chirps_short`,
  `ppt_anomaly`/`chirps_anom`, `ppt_poa`, `ppt_spp`, `spi`. Custom names
  resolve against `--theme-file` /
  `~/.config/weather-skills/plot.toml` `colormaps` (they used to be ignored).
  Every other variable uses `rocket`. Windrose uses a blue→orange
  speed palette; `--colormap` recolors the speed stacks. Quiver defaults to
  `YlGn` (S2S 10 m / 700 hPa wind-vector maps); `--colormap PiYG` matches their
  anomaly quivers. A variable with CF `flag_values` (e.g. `verify --metric hits`) uses a
  discrete colormap and labeled colorbar ticks; `--colormap` as comma-separated
  colors must then match the flag count.
  Discrete custom classes: `len(colors) == len(bounds) - 1` (one color per
  interval), or two extra colors packed as under + classes + over. Example
  spec: `{"theme": {"colormap": {"colors": ["white", "green", "navy"],
  "bounds": [0, 10, 50, 100], "under": "grey", "over": "magenta"}}}`.
- `--colormap-bounds` — comma-separated class stops (`0,10,50,100`). Folds
  into `theme.colormap.bounds` (a string `--colormap magma` becomes
  `{name, bounds}`). Combine with `--colormap white,green,blue` for a custom
  discrete scale, or with a matplotlib name to bin that cmap.
- `--colormap-under` / `--colormap-over` — colors for values below the first
  stop / above the last. Folds into `theme.colormap.under` / `.over`.
- `--cbar-ticks` / `--cbar-labels` — colorbar tick positions and labels
  (`layout.colorbar.ticks` / `.labels`). Labels require ticks and the same
  count. Example: `--cbar-ticks 0,50,100 --cbar-labels dry,ok,wet`.
- `--vmin` / `--vmax` — colorbar limits for heatmap, contour, quiver, and
  scatter. Either may be omitted (the unset end uses the data min/max).
  Setting either one drops the default discrete precip classes and
  stretches those colors (or `--colormap`) across the requested range.
  Values outside the range saturate and the colorbar gains an extend arrow.
  Diverging auto-symmetry (centered on zero) is skipped when either flag
  is set. Ignored for timeseries / xy / windrose. Cannot be used with CF
  `flag_values` fields. Layers may override with `::vmin=` / `::vmax=`.
- `--title` — optional figure title (above all panels). Prefer a short name
  that fits on one line (about 56 characters or less), e.g. `S2S precip`,
  not a full sentence. Longer titles wrap onto a second line at a `·` / `:` /
  word break.
- `--subplot-title` — optional map panel title, in panel order. Repeat the
  flag once per panel (`--subplot-title "Week 1" --subplot-title "Week 2"`).
  Fewer titles than panels keep the auto date/lead labels for the rest; more
  titles than panels is an error. An empty string hides that panel's title.
  On a single-panel map this is the axes title under `--title`. Heatmap /
  contour / quiver / layered maps — timeseries, xy, and windrose ignore it
  with a stderr warning (`--title` is their axes title).
- `--xlabel` / `--ylabel` — optional axis-label overrides. When omitted, maps
  use `Longitude` / `Latitude`, timeseries omits the x label when ticks are
  dates (otherwise the time dim) and uses the variable label on y, `xy`
  uses each series' variable label, and windrose uses `Frequency (%)`.
  Passed text is used as-is (not re-cased).
- `--cbar-label` — optional colorbar label. When omitted, the skill uses the
  variable `long_name` (then GRIB name, then the variable name) plus units
  (`Total precipitation [mm]`, `Wind speed [m/s]`, …). Layered maps: per-layer
  `--label` wins; `--cbar-label` fills in unlabeled heatmap/scatter/quiver
  layers. Ignored for timeseries / xy / windrose.
- `--index` — dim selections like `step=3,number=0`. A dim may take several
  comma-separated positions, e.g. `step=0,1,2`, which keeps the dim with just
  those positions. Negative positions are accepted and count from the end,
  Python-style (`step=-1` is the last step). Repeating a dim is an error, as
  are positions that address the same element — including negative aliases
  (`step=0,-3` on a 3-step axis). Heatmap list selections are only supported on
  the panel (step/time) dimension; other dims take a single position. Applied
  before panel layout: e.g. `--index step=2` reduces to a single-panel map at
  step 2, while `--index step=0,1,2` panels exactly those three steps;
  otherwise all steps are paneled. Panels follow the order given in the spec
  (`step=2,0` renders position 2 first). Heatmap, quiver, windrose, and `xy`
  apply the spec; `--kind timeseries` syntax-checks it, then ignores it with
  a stderr warning. Windrose flattens remaining positions into samples (a list
  like `step=0,1,2` keeps those steps in one rose, rather than panelling).
  `xy` reduces leftover dims after `--index` the same way as timeseries.
- `--reduce` — average over this dim for `--kind timeseries`. Repeatable, once
  per leftover non-time dim (`--reduce latitude --reduce longitude`). No dim is
  averaged unless you ask: a gridded input with no `--reduce`/`--along` is an
  error naming the dims that remain, so the figure never hides a data decision.
  Ignored by the map kinds (use `--index` to select there).
- `--along` — draw one `--kind timeseries` line per value of this dim instead
  of reducing it (`--along number` for ensemble members). Must not be the time
  axis. For band percentiles and per-trace styling, use `plot-timeseries`.
- `--extent` — heatmap/quiver map extent as `lon_min,lon_max,lat_min,lat_max`.
  Defaults to the data's cell-center min/max expanded by half the mean
  grid spacing on each side, so the view matches what `pcolormesh`
  actually draws (it treats coords as cell centers and extends ±½
  spacing).
- `--cities` — heatmap/quiver city overlay. Inline JSON like
  `'{"Windhoek": [-22.55, 17.08]}'` or a path to such a JSON file. Off by
  default.
- `--fontsize` — base font size for titles (including panel date labels), axis
  labels, city labels, and colorbar text (default 16). Raise on user request
  (e.g. `--fontsize 22`).
- `--figsize` — figure size in inches as `W,H` or `WxH` (e.g. `10,6`).
  When set, the PNG is that canvas at 150 dpi (legends stay inside it).
  When omitted, maps size from
  the geographic extent and crop tightly; timeseries is `10×6`, xy is `8×6`,
  and windrose is `8.5×7`.
- `--legend` — legend placement: a matplotlib loc (`best`, `upper right`,
  `upper left`, `lower left`, `lower right`, `right`, `center left`,
  `center right`, `lower center`, `upper center`, `center`), `outside right`,
  `below`, or `none`. Windrose default is `outside right`. Timeseries draws
  a legend only when this flag is set. Heatmap / contour / quiver / layered
  maps ignore it (they use a colorbar).
- `--rows` / `--columns` — heatmap/quiver panel grid. Pass either or both.
  Leftover cells stay blank when the grid is larger than the data: both
  given → `rows × columns` must be ≥ the number of panels (steps/times
  after `--index`); only `--columns` → rows = ceil(n / columns); only
  `--rows` → columns = ceil(n / rows). A grid smaller than the data is an
  error. When both are omitted, the default is up to 4 columns with extra
  rows as needed (blank leftover cells allowed). Heatmap and quiver —
  `--kind timeseries` and `--kind windrose` ignore them with a stderr
  warning.
- `--bbox` — optional `N/W/S/E` decimal degrees. Slices the gridded input to the
  bbox using `da.sel(...)` and sets the heatmap extent to that bbox. This is a
  rectangular slice (geographic overlays are decoration, not a mask). To
  restrict to a country, get its bbox from the `resolve-region` skill.
  Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing so
  global grids still intersect negative-lon bboxes. `--extent` (if passed) wins
  over the bbox-derived extent. Heatmap, quiver, windrose, and `xy` use it;
  `--kind timeseries` ignores `--bbox` with a stderr warning. Default unset
  → no slice.
- `--mask-geojson` — optional path to a GeoJSON boundary polygon (e.g. the
  `--geojson` output of the `resolve-region` skill). Gridded cells whose centers
  fall outside the polygon are set to NaN before plotting, so the heatmap shows
  the country shape rather than its bounding rectangle. All features in the file
  are unioned. Combine with `--bbox` to crop to the rectangle first,
  then mask to the polygon within it. Heatmap, quiver, windrose, and `xy` use
  it; `--kind timeseries` ignores it with a stderr warning. Default unset
  → no mask.
- `--draw-box` — optional black outline rectangle(s) drawn on each map panel.
  Same `N/W/S/E` form as `--bbox`. Repeat the flag for multiple boxes (e.g.
  IOD west `10/50/-10/70` and east `0/90/-10/110`). Unlike `--bbox`, this does
  **not** crop the data — it only overlays outlines. Antimeridian spans
  (`W > E`) are drawn as two segments. Heatmap and quiver — `--kind
  timeseries` and `--kind windrose` ignore it with a stderr warning. Default
  unset → no boxes.
- `--quiver-scale` — matplotlib quiver `scale` for `--kind quiver`. Larger
  values draw shorter arrows. When omitted, the skill sizes a typical
  (95th-percentile) wind to about 1.5× the subsampled grid spacing as a
  fraction of the map width, so 10 m/s basin winds stay readable. Pass `100`
  to match `plot_wind_and_sst_anomaly` (that default was tuned for small
  *anomaly* vectors, not full 10 m wind).
- `--quiver-step` — plot every Nth grid point for `--kind quiver`. Matches
  `plot_wind_and_sst_anomaly`'s `quiver_step`. When omitted, the skill uses
  stride 1 on ~1.5° (S2S) grids and auto-thins finer grids to about 1.5° so
  a GFS basin map looks like the S2S Indian Ocean wind overlay. Pass `1` to
  plot every native point.

### Output

A PNG at `--output`. The colorbar (and timeseries y-axis) label resolves
from variable attrs: `long_name` → `GRIB_name` → bare variable name →
`"value"`, suffixed with `[units]` when the `units` attr is present. Units
on the figure are a short display form (`mm/day`, `°C`, `mm`, `m/s`), not the
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

Multi-step forecast panel (precip uses the nested absolute-mm palette by default):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --kind heatmap --title "S2S precip"
```

Filled-contour map of the same field:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf_contour.png \
    --variable tp --kind contour --title "S2S precip"
```

Override the palette (e.g. magma):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --kind heatmap --colormap magma --title "S2S precip"
```

Pin the colorbar range (stretches the scale; values outside saturate):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf.png \
    --variable tp --kind heatmap --vmin 0 --vmax 50 --title "S2S precip"
```

Six weekly maps in two rows of three (a 5-step cube on the same 2×3 grid leaves one panel blank):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/weekly.zarr -o /tmp/weekly.png \
    --variable tp --rows 2 --columns 3
```

Custom figure title, panel titles, axis labels, and colorbar text:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/weekly.zarr -o /tmp/weekly.png \
    --variable tp --title "Kenya rainfall" \
    --subplot-title "Week 1" --subplot-title "Week 2" \
    --xlabel "Lon" --ylabel "Lat" --cbar-label "Rain (mm)"
```

Single-step map with cities and an explicit extent:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ecmwf_step0.png \
    --variable tp --index step=0 \
    --extent 11,29,-30,-15 \
    --cities '{"Windhoek": [-22.55, 17.08]}'
```

Country-shaped map masked to a boundary polygon:
```bash
# After resolve-region writes --geojson /tmp/kenya.geojson (dummy bbox below):
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/chirps_kenya.zarr -o /tmp/kenya.png \
    --variable precip --bbox 5/34/-5/42 --mask-geojson /tmp/kenya.geojson
```

Indian Ocean map with IOD west/east dipole boxes overlaid:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ts_anom.zarr -o /tmp/iod_boxes.png \
    --variable ts_anomaly --extent 40,120,-20,20 \
    --draw-box 10/50/-10/70 --draw-box 0/90/-10/110
```

Precip heatmap with station scatter and a country outline on the same axes:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -o /tmp/imerg_vs_tahmo.png \
    --layer heatmap:/tmp/imerg.zarr::variable=precip \
    --layer scatter:/tmp/tahmo.zarr::variable=precip \
    --layer outline:/tmp/kenya.geojson \
    --title "IMERG vs TAHMO"
```

Time series (say which dims to collapse — nothing is averaged silently):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ts.png \
    --variable tp --kind timeseries --reduce latitude --reduce longitude
```

One line per ensemble member instead of collapsing them:
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/ecmwf_namibia.zarr -o /tmp/ts_members.png \
    --variable tp --kind timeseries \
    --reduce latitude --reduce longitude --along number
```

XY scatter (September IOD vs October rainfall, one point per year):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py --kind xy \
  --x /tmp/iod_sep.zarr --y /tmp/rain_oct.zarr \
  --pair-on year \
  --xlabel "September IOD" --ylabel "October rainfall" \
  -o /tmp/iod_vs_rain.png
```

Wind rose from 10 m u/v (auto-detected `u10`/`v10`):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/era5_wind.zarr -o /tmp/windrose.png \
    --kind windrose --bbox 5/34/-5/42 --title "Kenya 10 m wind"
```

S2S-style 10 m wind-vector map (YlGn speed + quiver, one panel per step):
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot.py -i /tmp/s2s_10wind.zarr -o /tmp/10m-wind_vectors.png \
    --kind quiver --bbox 5/34/-5/42 --title "10 m wind"
```

## Inspect and edit a plot (stateless)

PNG is the only artifact. There is no `*.plot.json` sidecar.

Every kind compiles a small weather-skills JSON spec (Zarr paths and layout,
not the raster `z` grid). The first PNG does not need `--spec`: pass
`--title`, `--variable`, `--mask-geojson`, `--figsize`, and the rest as CLI
flags.

1. `plot -i data.zarr -o out.png --title "Precip" --variable precip`
2. If a knob is not a CLI flag (or you need to see the resolved object),
   re-run the same command with `--dump-spec -` and read stdout. Do this
   only when needed — the full JSON is token-expensive.
3. Re-run the same CLI plus `--patch '{"axes": {"xticks": [...]}}'`.
   Do not pass the full dumped spec back unless you are replaying it with
   `--spec`.

`--patch` merges onto this invocation's spec (CLI-built, or `--spec` if you
passed one). A `patch` key inside a spec object is rejected.
`--dump-spec PATH` writes a file for tests or local editing; agents should
prefer stdout (`-`) then `--patch`.

`--patch '{"title": "Edited"}'` sets spec values without a dump —
handy for title, annotations, shapes, axis-label position, and colorbar size.
Shorten a colorbar with
`--patch '{"layout": {"colorbar": {"len": 0.45, "thickness": 12}}}'`.
Move a windrose radial label with
`--patch '{"axes": {"ylabel": {"coords": [1.15, 0.5], "rotation": 0}}}'`.

## Matplotlib options in the spec

Values must be JSON (strings, numbers, bools, lists, objects). Unknown keys
on artist/axes objects are errors. There is no `eval` and no Python callables.

Put matplotlib `rcParams` in `theme.rc`. They apply after
the seaborn theme, so they win. Backend / interactive keys (`backend`,
`interactive`, `tk.*`, …) are rejected.

| Spec key | Matplotlib surface |
| --- | --- |
| `axes` | Matplotlib Axes config applied after the data are drawn: scales, limits, labels, **ticks** (`xticks`/`yticks` lists or `{values, labels}`), locators, formatters, spines, grid, legend, twins. `xlabel` / `ylabel` may be a string or `{text, loc, pad, coords, rotation, ha, va, …}` (`coords` is `[x, y]` in axes fraction; omit `text` to keep the already-drawn label). Same object on every figure skill. A dump includes only the keys you set. |
| `annotations` | `ax.text` or `ax.annotate` (`xy`, `xytext`, `arrowprops`, fonts, `bbox`). `xref: paper` / `transform: axes` uses axes fraction. `axes`/`panel` picks a subplot |
| `shapes` | `rect`, `hline`, `vline`, `hspan`, `vspan`, `line`, `circle`/`ellipse` |
| `traces[].line` / `.mesh` / `.contour` / `.scatter` / `.bar` / `.quiver` / `.windrose` | kwargs for the matching artist (`linewidth`, `alpha`, `marker`, `shading`, `levels`, `scale`, `nsector`, …). `contour.lines: false` skips isoline overlay |
| `traces[].fill` | `fill_between` for `--band` |
| `traces[].mediogram` | `{width, forecast, mclimate, mean, legend}` for box colors / mean line |
| `layout.colorbar` | `extend`, `pad`, `orientation`, `location`, plus `len`/`thickness` |
| `layout.facecolor`, `layout.dpi` | figure patch and DPI |
| `theme.rc` | matplotlib rcParams, applied after the seaborn theme |

Every knob has exactly one home, and an unknown key is an error naming the
canonical path, so an edit never silently does nothing. Artist kwargs live on
the trace that draws them rather than at the top level.

A timeseries series can plot on a twin y-axis with `"twin": "y"`.
Every kind (heatmap, contour, timeseries, xy, windrose, quiver, `--layer`)
dumps the same `axes` / `annotations` / `shapes` / `theme.rc` objects and
applies them after the data are drawn. Reposition a polar windrose frequency
label with `--patch '{"axes": {"ylabel": {"coords": [1.15, 0.5], "rotation": 0}}}'`.
The full key table is in [`docs/plotting.md`](../../docs/plotting.md).

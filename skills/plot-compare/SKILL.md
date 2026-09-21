---
name: plot-compare
description: Render a side-by-side two-row comparison PNG of two weather-skills standard dataset Zarr stores (gridded-vs-gridded or station-vs-gridded as separate rows, not overlaid). Use for sat-vs-station validation, model-vs-obs comparison, or cross-source QC. To overlay stations on a heatmap, use plot --layer instead. For precipitation, convert-to-totals after aggregate-temporal before plotting. Use --fontsize to enlarge panel titles, row labels, ticks, and colorbars (default 16).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot-compare

Source-agnostic two-dataset visualization. Produces a 2-row figure
with one panel per time slice; row A is one input, row B the other.
Handles:

- Gridded vs. gridded (pcolormesh maps).
- Station (`station_id`-indexed) vs. gridded as **two rows** (scatter in one
  row, pcolormesh in the other — not overlaid on the same axes). To draw
  stations on top of a heatmap, use `plot --layer heatmap:… --layer scatter:…`.

When exactly one input is a point_obs Zarr, that input is placed
on the top row to match the canonical "stations vs. satellite" layout.

The two inputs must already be at the same time resolution and are
compared on the time bins they share. `plot-compare` intersects the two
axes' bin labels and renders the last `N` of the COMMON labels, selecting
those same labels from both inputs so panel `i` shows the same time window
for both rows. A reporting-latency offset that drops one input's trailing
bin (e.g. a station whose final week is not yet in) is not an error — it
just yields one fewer common bin. `plot-compare` exits non-zero only when
the two axes are at different resolutions (different median bin width, or
one a calendar `time` axis and the other a forecast `step` axis) or have
no overlapping bins; in either case it asks you to aggregate to a common
resolution first. To compare data captured at different cadences (e.g.
daily station observations against weekly or dekadal gridded
rates), aggregate each input to the same window with the
`aggregate-temporal` skill before comparing, then `convert-to-totals` so
precipitation figures are period amounts (`mm`), not rates.

Each row can draw a different variable: `--variable-a`/`--variable-b`
select per-row, with `--variable` as a both-rows shorthand. This lets
you compare different quantities (e.g. soil moisture vs. precipitation)
on one figure.

The color scale adapts to what is being compared. When both rows resolve
to the same variable and matching units, one shared scale is used (for
precipitation, the nested absolute-mm total classes or CHIRPS anomaly classes,
so values are visually comparable across rows). When the rows are different
variables or have differing units, each row gets its own independent
scale, colormap, and labeled colorbar — rainfall still uses the nested
absolute-mm classes. `--shared-scale` and
`--independent-scale` force either mode. Country outlines come from the
bundled Natural Earth GeoJSON (same store as `resolve-region`), compiled
through matplotlib.

Both rows always share the gridded input's spatial extent so the figure
is centered on the gridded base; station points outside that extent are
clipped by the shared lon/lat range.

Panel titles render the time-bin range as `30 Apr–9 May '26`
with the bin coord interpreted as the inclusive **left** edge: end =
start + bin_width − 1 day. Matches `aggregate-temporal` and
`deaccumulate`'s period-start convention so a 10-day dekad starting
`2026-04-30` renders as `30 Apr–9 May '26` (10 days inclusive).

## When to use

- Validating a satellite product against station observations for a country.
- Comparing two forecasts (e.g. model A vs. model B) on the same axes.

For N gridded datasets as a valid-time grid with blank cells where a
dataset has no matching time, use `plot-compare-forecasts`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare.py -i <a.zarr> -i <b.zarr> --output <out.png> \
    [--variable NAME] [--variable-a NAME] [--variable-b NAME] \
    [--colormap NAME] [--colormap-bounds 0,10,50] [--cbar-ticks N,...] [--cbar-labels TEXT,...] \
    [--colormap-a NAME] [--colormap-b NAME] [--vmin N] [--vmax N] \
    [--shared-scale | --independent-scale] [--title TEXT] [--xlabel TEXT] [--fontsize N] [--figsize W,H] \
    [--panel-spacing W[,H]] [--panels N] [--time-dim DIM] \
    [--bbox N/W/S/E] [--mask-geojson PATH] \
    [--spec PATH_OR_JSON] [--patch PATH_OR_JSON] [--dump-spec -|PATH]

uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare.py \
    -i <a.zarr> -i <b.zarr> -o <out.png> --patch '{"title": "Edited"}'
```

### Arguments
- `--input`, `-i` — pass exactly twice. The first input is row A, the second is row B. Station-schema is allowed on either. Optional when `--spec` already lists both paths.
- `--spec` — optional full plot spec JSON (file or inline). First runs are
  CLI flags only. Prefer `--patch` for edits. Pass `--spec` only when
  replaying a dumped object. Spec input paths are opened as Datasets so
  provenance chains from the Zarr.
- `--patch` — optional JSON (file or inline) deep-merged onto this run's spec
  before CLI flags overlay. Same knobs as `--spec`. A `patch` key inside a
  spec object is rejected.
- `--dump-spec` — dump the assembled plot spec as JSON and skip drawing a
  PNG. `--output` is not required. Bare `--dump-spec` (or `-`) prints to
  stdout; a path writes a file. Token-expensive; omit unless `--patch` needs
  a key you cannot name from the CLI.
- `--label` — row label for each `--input`, in order. When omitted, labels are
  inferred from provenance (`weather_skills_source`, fetch skill history).
- `--output`, `-o` — PNG path.
- `--variable`, `-v` — variable for both rows. Per-row `--variable-a`/`-b`
  override it. Each resolved variable must exist in its own input.
- `--variable-a` — variable for row A (overrides `--variable` for that row).
  Default per row: `--variable`, else that input's first real data var
  (CF grid-mapping/CRS container vars such as `latitude_longitude` are
  skipped during auto-pick).
- `--variable-b` — variable for row B (same resolution as `--variable-a`).
- `--colormap` — matplotlib colormap name, comma-separated colors, or a
  `{colors, bounds}` object (also `--colormap-bounds` / `--cbar-ticks` /
  `--cbar-labels`). When omitted, precipitation totals use the nested
  absolute-mm classes (`ppt_daily` / `ppt_week` / `ppt_month` / `ppt_season`);
  anomalies use the diverging CHIRPS classes. When plotting rainfall
  anomalies, omit `--colormap` so those classes apply. Negative `--vmin` /
  `--colormap-bounds` need the equals form (`--vmin=-50`). In independent-scale
  mode a non-precip row falls back to `viridis`.
- `--colormap-a` / `--colormap-b` — per-row matplotlib colormap name or
  comma-separated colors in independent-scale mode. Precedence per row:
  `--colormap-a`/`-b`, then `--colormap`, then the nested precip total /
  CHIRPS anomaly classes or `viridis`.
- `--vmin` / `--vmax` — shared colorbar limits. Either may be omitted
  (the unset end uses the data min/max). Setting either one drops the
  default discrete precip classes and stretches those colors (or
  `--colormap`) across the requested range. In independent-scale mode the
  same limits apply to both rows.
- `--shared-scale` / `--independent-scale` — mutually exclusive; force one
  shared color scale across both rows or a per-row scale + colorbar. When
  neither is given, the mode is chosen automatically: shared when both
  rows resolve to the same variable AND matching units, else independent.
- `--title` — figure title. Long titles wrap onto a second line.
- `--xlabel` — override the bottom longitude axis label (default `Longitude`).
  Row titles stay `--label`.
- `--fontsize` — base font size for panel titles, row labels, ticks, and
  colorbars (default 16). Raise on user request (e.g. `--fontsize 18`).
- `--figsize` — figure size in inches as `W,H` or `WxH` (e.g. `16,8`).
  When set, the PNG is that canvas at 150 dpi. Default `22×10`, cropped tightly.
  Equal-aspect map panels are packed tightly unless `--panel-spacing` is set.
- `--panel-spacing` — gap between panels as a fraction of panel size (`W` or
  `W,H`; matplotlib `GridSpec` `wspace` / `hspace`). One value sets both
  axes. Writes `layout.facet.wspace` / `hspace`. Same keys work via `--patch`.
- `--panels` — number of panels per row (default 3).
- `--time-dim` — override the time axis. Defaults to `time` if present, else `step`.
- `--bbox` — optional `N/W/S/E` decimal degrees. Rectangular clipping:
  gridded inputs get a `ds.sel(...)` slice to the bbox and station inputs
  are filtered to the bbox (no polygon test); axes are set to the bbox.
  To restrict to a country, get its bbox from the `resolve-region` skill. Longitudes in
  `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing so global
  grids intersect negative-lon bboxes. Default unset → no slice.
- `--mask-geojson` — optional path to a GeoJSON boundary polygon. Gridded
  inputs get a `shapely.contains_xy` polygon mask that NaN's cells outside
  the polygon (station inputs are unaffected). Use `resolve-region`'s
  `--geojson` output to produce a country polygon. May be combined with
  `--bbox` (slice then mask) or used alone (mask, no rectangular slice).
  Admin-1 boundary overlay is drawn on top as decoration regardless.

### Behavior

- **Shared-resolution, overlapping bins.** The two inputs must already be
  at the same time resolution; `plot-compare` compares them on their
  overlapping bins. It checks that the two axes are the same kind (both a
  calendar `time` axis, or both a forecast `step` axis — compared within
  the native dtype, never cross-cast) and share a median bin width, then
  intersects the bin labels (matched within a small fraction of the bin
  width) and renders the last `N` of the COMMON labels, selecting the same
  labels from both inputs so each panel shows the same window for both
  rows. A latency offset that drops one input's trailing bin is tolerated
  (one fewer common bin). The run exits non-zero only on a resolution
  mismatch ("different time resolutions; aggregate to a common resolution
  first, e.g. with the `aggregate-temporal` skill") or an empty
  intersection ("no overlapping time bins"). `plot-compare` performs no
  temporal aggregation or unit transformation of its own.
- **Admin-polygon clipping.** The Natural Earth admin-1 GeoDataFrame
  is spatially clipped (`gdf.clip(box(*gridded_bbox))`) so polygons
  that straddle the bbox edge are truncated at the edge rather than
  rendered whole. Empty geometries produced by the clip are dropped.
- **Shared spatial extent.** Both rows' `set_xlim`/`set_ylim` come
  from the gridded input's lat/lon bounds, not from each row's own
  data bounds. Station scatter points outside that extent are clipped.
- **Longitude wrap before bbox slice.** When `--bbox` or `--mask-geojson`
  is set and a gridded input has lon in `[0, 360]`, lons are auto-wrapped
  to `[-180, 180]` (and the dim re-sorted) before the rectangular slice
  and polygon mask. Inputs already in `[-180, 180]` are unaffected.
- **Color-scale mode.** By default the scale is shared when both rows
  resolve to the same variable AND matching (stripped) `units`, and
  independent otherwise. `--shared-scale` / `--independent-scale` force
  the mode. In shared mode both rows use one colormap (a matplotlib name
  or comma-separated colors), normalization, vmin, and vmax. In independent
  mode each row computes its own vmin/vmax
  from its own data, uses its own colormap (precedence `--colormap-a`/`-b`,
  then `--colormap`, then `viridis`) with a continuous norm, and gets its
  own colorbar labeled `{file} {long_name} [{units}]` (`long_name`, then
  `GRIB_name`, then the variable name). Shared-scale colorbars include
  units too. Units on the figure are a short display form (`mm/day`,
  `°C`), not the on-disk CF string.
- **Input units.** In shared mode, when the two rows carry differing
  `units`, the figure colors values from different units on a single
  scale, so a warning naming both units is printed to stderr. This is a
  rendering caveat only — the figure is still produced and the exit status
  is 0. The check applies only when both rows carry a string `units` attr;
  a missing value is not compared. In independent mode each row has its
  own scale, so no cross-row units warning is emitted.

### Output

A PNG with a `(2, n)` `GridSpec` (default `figsize=(22, 10)`; override with
`--figsize`; compressed layout packs the equal-aspect maps). Each row gets its own colorbar.
Station scatter points use `s=30`. Each panel's y-axis is the row's
dataset name (`weather_skills_source`, else `A` / `B`). Latitude ticks
stay on the leftmost panel of each row.

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata. Read-back:

```bash
python3 -c "from PIL import Image; import json; img=Image.open('out.png'); print(json.loads(img.info['weather_skills_history']))"
```

Or:

```bash
exiftool out.png
```

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_compare.py -i /tmp/tahmo_dekadal.zarr -i /tmp/imerg_dekadal.zarr \
    --variable precip --output /tmp/sat_vs_station.png \
    --title "IMERG vs TAHMO dekadal"
```

Both inputs are on the same dekadal axis here: the station `tahmo.zarr`
was aggregated to `tahmo_dekadal.zarr` with the `aggregate-temporal`
skill (same period/method/anchor as the IMERG dekadal aggregation)
before comparing.

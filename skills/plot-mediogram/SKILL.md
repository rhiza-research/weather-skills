---
name: plot-mediogram
description: Render an ECMWF-style mediogram PNG comparing a forecast ensemble against an m-climate ensemble at one point. Pass the forecast Zarr then the m-climate Zarr with -i. Set geo.lat, geo.lon, and any other parameters in --spec. Grouped box plots per step (forecast cyan, m-climate red) plus the forecast mean line. For precipitation, run convert-to-totals after aggregate-temporal before plotting.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_mediogram.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot-mediogram

Single-point mediogram plotting an ECMWF ensemble forecast distribution against an m-climate (historical) ensemble distribution. For each forecast step, two side-by-side box plots are drawn (forecast left/cyan, m-climate right/red) with the forecast ensemble mean as a black line.

## Input schema

Both inputs are Zarr stores with at least:
- a `number` dim (ensemble members)
- a `step` dim (forecast lead time)
- spatial coords identifiable as `latitude`/`longitude` (CF-style or common aliases)
- at least one data variable

Lat/lon selection is nearest-neighbor.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_mediogram.py \
    -i <forecast.zarr> -i <mclimate.zarr> -o <out.png> \
    --spec '{"geo":{"lat":-1.3,"lon":36.8},"inputs":[{"variable":"tp"}],"title":"Nairobi"}'
```

### Arguments

- `--input`, `-i` — pass exactly twice: forecast Zarr first, m-climate Zarr second. Optional when `--spec` lists both paths.
- `--output`, `-o` — PNG path.
- `--spec` — JSON object or path, always deep-merged onto the spec built from the opened files. Your values win. `geo.lat` and `geo.lon` are required. A `patch` key inside the object is rejected.
- `--dump-spec` — write the merged spec as JSON and skip the PNG. Bare `--dump-spec` or `-` prints to stdout.

### Parameters (`--spec`)

- `geo.lat`, `geo.lon` — point, nearest-neighbor.
- `inputs[0].variable`, `title`, `xlabel`, `ylabel`.
- `theme.fontsize` (default 16), `layout.figsize` as `[W, H]` (default about 10×5).

### Output

A PNG at `--output`, single axes, default figsize `(10, 5)` (override with
`layout.figsize`), legend below the boxes. Stdout prints `plot hash` (sha256 of
RGB pixels) and `data: not null` or `data: NULL`. Compare hashes across
runs; `NULL` means inspect-zarr the inputs. Look at the PNG as well.
`--dump-spec` skips the PNG and this report. Up to 6 forecast steps on the x-axis labeled with actual leads (`+7d`, `+10d`, …). The y-axis (and default title) use the variable `long_name`.

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
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_mediogram.py \
    -i /tmp/ecmwf_forecast.zarr \
    -i /tmp/ecmwf_mclimate.zarr \
    -o /tmp/mediogram_nairobi.png \
    --spec '{"geo":{"lat":-1.3,"lon":36.8},"inputs":[{"variable":"tp"}]}'
```

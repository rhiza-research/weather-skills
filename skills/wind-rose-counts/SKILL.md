---
name: wind-rose-counts
description: "Count wind samples by direction sector (16 compass points by default) and speed class from the wind speed and from-direction of a gridded or station weather-skills Zarr, as counts and percent frequencies with calms apart: the table behind a wind rose. Use when a task needs wind-rose numbers, prevailing wind directions, or a wind frequency table for a station or place."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/wind_rose_counts.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# wind-rose-counts

Pools every wind sample (all times, cells, members) into a direction-by-speed table of counts and percent frequencies.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/wind_rose_counts.py -i IN.zarr -o OUT.zarr [--speed VAR] [--direction VAR] [--sectors N] [--speed-edges E0,E1,...] [--calm-speed SPEED] [--keep DIM ...]
```

### Arguments
- `--input`, `-i` — input Zarr, gridded or station, holding a wind speed and the direction it blows from. `wind-speed-direction` makes both from u and v.
- `--speed` — wind speed variable. Default `wind_speed`.
- `--direction` — direction the wind blows from, in `degree` clockwise from north. Default `wind_from_direction`.
- `--sectors` — number of direction sectors, the first centred on north. Default `16` (22.5° each). Fewer than 4 exits 2.
- `--speed-edges` — comma-separated lower edges of the speed classes in the speed's units, starting at `0` and increasing; the last class has no upper limit. Default `0,2,4,6,8,10,12` for `m s-1`; other units need this flag.
- `--calm-speed` — speeds at or below this count as calm. Default `1e-6`, as in `wind-speed-direction`; pass the value given there if it was larger.
- `--keep` — a dim to keep rather than pool (repeatable), e.g. `station_id` for one table per station. Default: pool every dim.
- `--output`, `-o` — output Zarr.

### Output
Dims `(*keep, direction, speed_bin)`:
- `count` — samples in each sector and speed class (units `1`).
- `frequency` — `count` as a percent of all samples, calms included, so the table plus `calm_frequency` sums to 100.
- `calm_count`, `calm_frequency` — calm samples, on the `--keep` dims only.
- `sample_count` — calm samples plus samples in the table.
- Coords: `direction` is each sector's centre in degrees (0 = north, 90 = east); `speed_bin` is each class's lower edge and `speed_upper` its exclusive upper edge (`inf` for the last), in the speed's units.

### Notes
- A sector runs from half a sector before its centre to half after: with 16 sectors, `0` holds 348.75° to 11.25°.
- NaN speeds are skipped. A speed above `--calm-speed` with a NaN direction is missing, not calm: it is left out and counted on stderr.
- For one place, `select` or `point-value` the cell first; pooling a grid mixes winds from every cell. For one season, `select` its times first.
- `csv-io` exports the table, one row per sector and speed class.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/wind_rose_counts.py -i nairobi_wind.zarr -o nairobi_rose.zarr
```
Writes the 16-sector by 2 m s-1 class counts and percent frequencies of Nairobi's winds, pooled over all times.

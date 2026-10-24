---
name: difference
description: "Subtract one weather-skills Zarr from another (A − B), cell by cell, keeping only the coordinates both share and broadcasting over dims only A or only B has. Use for anomalies (a field minus its baseline mean), change maps (scenario minus historical), or forecast minus observations."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py *)
metadata:
  version: "0.0.5"
  catalog-group: transforms
---

# difference

Writes A − B for each data variable the two inputs share.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i A.zarr -i B.zarr -o OUT.zarr [-v VAR ...]
```

### Arguments
- `--input`, `-i` — pass exactly twice: first A (the field), then B (what is subtracted).
- `--output`, `-o` — output Zarr, replaced if it exists. Do not point it at an input.
- `--variable`, `-v` — difference only these variables; each must be in both inputs. Repeatable. Default: every data variable the inputs share.

### Output
- One variable per differenced name, holding A − B on the aligned dims. Variables not differenced are dropped.
- Each variable keeps A's attrs only (B's `aggregation_period`, `cell_methods` are not copied); dataset attrs come from A.
- `long_name` gains ` difference`: `Sea Surface Temperature difference`, also when B is a climatology. To call it an anomaly, set that in the figure's colorbar title. `standard_name` is kept.
- B is converted to A's units before subtracting (°C minus K works). A temperature difference is written in `delta_degC`, the unit of a temperature change (1 delta_degC = 1 K).
- Boolean and unsigned-integer inputs become signed integers, so negative results stay negative.

### Notes
- Shared dims keep only labels present in both inputs (an inner join). Lat/lon are rounded to 5 decimals on open, but offset grids (CHIRPS 0.05° centres vs a 0.1° grid) share few labels: put both on one exact grid first. No overlap along a dim exits 2.
- A dim on only one side broadcasts: `(time, latitude, longitude)` minus `(latitude, longitude)` gives one anomaly per time. If A and B each have a dim the other lacks (`time` vs `dayofyear`, `step` vs `time`), it exits 2; put both on the same axis first.
- A shared dim with no coordinate pairs by position; different sizes exit 2.
- Incompatible units (`mm day-1` minus `mm`) exit 2, as do precip totals over different `aggregation_period`s (a 7-day minus a 30-day total).
- No shared variable exits 2; rename one input's variable first with edit-coords `--rename`.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i chirps_kenya_weekly.zarr -i chirps_kenya_clim.zarr \
  -o chirps_kenya_anom.zarr
```
Writes weekly CHIRPS rainfall anomalies over Kenya: each week minus the climatology on the same weeks.

## Details

When plotting rainfall anomalies, omit `theme.colormap` so the default diverging millimetre classes apply. A projected-change map is the same call with a scenario time-mean as A and the historical time-mean as B.

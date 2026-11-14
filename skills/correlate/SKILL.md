---
name: correlate
description: "Correlate two weather-skills Zarrs along a dimension (default time), cell by cell or station by station: Pearson or Spearman r, a two-sided p-value, and the number of pairs. A 1-D series such as a climate index broadcasts across a grid. Use for forecast–observation anomaly correlation over many inits, teleconnection maps (Niño3.4 or IOD against rainfall), or spatial pattern correlation."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/correlate.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# correlate

Correlates input A with input B along one or more dims and writes the correlation field.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/correlate.py -i A.zarr -i B.zarr -o OUT.zarr [--variable-a NAME] [--variable-b NAME] [--dim time ...] [--method pearson|spearman]
```

### Arguments
- `--input`, `-i` — pass exactly twice: A, then B.
- `--variable-a`, `--variable-b` — variable in A and in B (names may differ). Default: each input's first usable variable.
- `--dim` — dim to correlate along; repeat for several (`--dim latitude --dim longitude` for a spatial pattern correlation per time). Default: `time`. Both inputs need every `--dim`.
- `--method` — `pearson` (linear, default) or `spearman` (rank; better for skewed rainfall).
- `--output`, `-o` — output Zarr.

### Output
- `correlation` — r, units `1`, on every dim except the `--dim`s.
- `p_value` — two-sided p-value from the t-test on r with n − 2 degrees of freedom. It assumes independent pairs; overlapping weeks or autocorrelated series make it too small.
- `n_pairs` — how many pairs had both values.
- Stdout prints r and p for a single value, or the mean and range of r over cells.

### Notes
- Only pairs with both values are used; fewer than 3 pairs leaves r and p NaN. Missing values are never filled.
- Shared dims keep labels present in both inputs (inner join); no overlap exits 2. A dim on only one side broadcasts (an index with `time` against a `time, latitude, longitude` grid). If each input has a dim the other lacks, it exits 2.
- An ensemble `number` dim exits 2: take the ensemble mean first with `summarize-dim`.
- Correlate anomalies, not raw values, when a seasonal cycle would dominate: subtract a climatology with `difference` (or `standardize-anomaly`) first.
- Units are ignored; r is dimensionless.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/correlate.py -i nino34_monthly.zarr -i chirps_eastafrica_ond_anom.zarr \
  -o nino34_rain_r.zarr --method spearman
```
Writes the rank correlation between Niño3.4 and OND rainfall anomalies at each grid cell, with p-values.

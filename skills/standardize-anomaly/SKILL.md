---
name: standardize-anomaly
description: "Compute a standardized anomaly (z-score), (field − mean) / std, from a data Zarr and a climatology Zarr holding the mean and standard deviation. The result is dimensionless, so unusualness compares across variables and places. Use for anomaly detection and SPI/SPEI-style standardized indices."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py *)
metadata:
  version: "0.0.3"
  catalog-group: transforms
---

# standardize-anomaly

Writes `(NAME − NAME_avg) / NAME_std` for each requested variable, using the climatology's mean (`NAME_avg`) and standard deviation (`NAME_std`).

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py -i IN.zarr --climatology CLIM.zarr -v NAME [-v NAME ...] -o OUT.zarr
```

### Arguments
- `--input`, `-i` — data Zarr holding `NAME`.
- `--climatology` — climatology Zarr holding `NAME_avg` and `NAME_std` for every `NAME`.
- `--variable`, `-v` — variable to standardize; repeatable, required. All names are checked up front; anything missing from either input exits 2 with the full list.
- `--output`, `-o` — output Zarr.

### Output
- One `NAME_anomaly` per variable, `units: "1"`, `long_name: "NAME standardized anomaly"`. The field's other attrs are not copied.
- Spatial dims are named `latitude` / `longitude` whatever the inputs called them.
- Cells where `NAME_std` is 0 are NaN (no spread, so no z-score). NaN in any of the three gives NaN; nothing is filled.

### Notes
- Labels must match exactly on shared dims, with `time` matched by timestamp. Every input label needs a climatology match; partial coverage, sub-daily input against a daily climatology, or noon vs midnight stamps exit 2. Extra climatology labels are dropped.
- Dims only on the input (`number`) broadcast. A climatology dim the input lacks exits 2; select one entry of it first.
- A forecast lead dim (`step`) exits 2 unless the climatology has it too. Convert leads to valid times first.
- The climatology is converted to the field's units: the mean as a value (K → °C subtracts 273.15), the std as a spread (1 K = 1 °C). Incompatible units, or units on only some of the three variables, exit 2.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py -i chirps_kenya_2026-10.zarr \
  --climatology chirps_kenya_clim.zarr -v precip -o chirps_kenya_zscore.zarr
```
Writes daily CHIRPS precip z-scores over Kenya for October 2026 against the day-of-year climatology.

## Details

A plain anomaly (field − mean) keeps the field's units; this skill divides by the std so a +2 means the same for rainfall in Nairobi as for temperature in Lagos. To standardize a weekly rainfall total, build the climatology as weekly-total `precip_avg` / `precip_std` on the same week stamps as the observed weekly totals.

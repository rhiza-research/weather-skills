---
name: drop
description: Drop named data variables or non-index coordinates from a weather-skills standard dataset Zarr (repeatable --name). Use to strip leftovers that block concat or difference — e.g. valid_time, surface, heightAboveGround, spatial_ref — or to keep only the variables a later step needs. Cleans up CF bounds and grid_mapping/coordinates references. Refuses dimension index coordinates (use select or summarize-dim) and dropping every data variable.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/drop.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# drop

Removal primitive. Drops the named data variables and non-index coordinates
(`ds.drop_vars`) and writes a new standard dataset. Everything else passes
through unchanged.

## When to use

- `concat` or `difference` fails or misaligns because one input carries extra
  scalar or auxiliary coordinates (`valid_time`, `surface`, `spatial_ref`, ...).
  Run `inspect-zarr` to see them, then list each one with `--name`.
- A dataset has variables a later step does not need.

To remove a dimension, collapse it with `select` (pick one entry) or reduce
over it with `summarize-dim`. To rename instead of drop, use `edit-coords`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/drop.py --input <in.zarr> --output <out.zarr> \
    --name NAME [--name NAME ...]
```

### Arguments
- `--input`, `-i` — input Zarr containing a weather-skills standard dataset.
- `--output`, `-o` — output Zarr.
- `--name` — repeatable, required. A data variable or a coordinate that is not
  a dimension's index.

### Output

The input without the named variables. A dropped coordinate's CF `bounds`
variable is dropped with it. The dropped names are removed from every
`coordinates`, `bounds`, and `ancillary_variables` attribute, and a
`grid_mapping` that named a dropped variable (e.g. `spatial_ref`) is removed.

The skill exits with code 2 and a clear message when: a name does not exist
(the message lists the data variables and coordinates); a name is a
dimension's index coordinate; or the drop would leave no data variables.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `weather_skills_history` and appends its own entry; `args`
records `name` as the list given.

## Example

An IFS fetch carries scalar `valid_time` and `surface` coordinates that the
IMERG store lacks. Drop them, then concatenate:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/drop.py -i /tmp/ifs.zarr -o /tmp/ifs_clean.zarr \
    --name valid_time --name surface
```

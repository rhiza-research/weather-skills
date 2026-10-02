---
name: concat
description: Concatenate two or more weather-skills standard dataset Zarr stores along a named dimension, optionally assigning coordinate values to the new axis. Use when combining ensemble members, stitching time windows, or merging per-country fetches into a single dataset.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# concat

Source-agnostic concatenation along a named dim. Inputs must share all other dims and variables. If the concat dim does not exist on the inputs, a new dim is created (use `--coords` to assign values).

## When to use

- Stitching per-country or per-date fetches into one Zarr.
- Joining two ensemble halves (cf and pf) along `number`.
- Glueing back two time windows after parallel processing.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i a.zarr -i b.zarr [-i ...] --dim DIM --output <out.zarr> \
    [--coords V1,V2,...]
```

### Arguments
- `--input`, `-i` — input Zarr (pass once per input; order is preserved). At least two are required.
- `--dim` — dimension name to concatenate along.
- `--coords` — optional comma-separated coord values to assign to the new dim. Length must match number of inputs; only used when `--dim` does not already exist on inputs.
- `--output`, `-o` — output Zarr.

### Input units

Concatenation combines the inputs into one array that carries a single `units`
label (the first input's attrs are preserved on the output). For each data
variable common to every input, the `units` attr is compared across the inputs
before any output is written. When two inputs carry the variable in differing
units, the skill exits with status 2 and names each input and its units; no
output is produced. Inputs that omit `units` for a variable are not treated as a
violation — only present values are compared.

### Output

A single Zarr with the concat dim extended. Attrs from the first input are preserved.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` DAG. Concat is a
join: the top-level array is this concat entry, and `input` is a list of
`{basename, hash, history}` dicts (one per input, in command-line order).
Each item's `history` is that input's full provenance subgraph (empty when
the input had none), so every parent branch is recorded equally — the first
input is not flattened into a linear spine. The entry also records the git
`commit` of the skill that ran. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `version` is the `_SKILL_VERSION` constant
in `scripts/concat.py`. Each input's `hash` is a sha256 over its stored
bytes, so renamed-but-unchanged inputs still match and same-named-but-modified
inputs do not.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i /tmp/cf.zarr -i /tmp/pf.zarr --dim number --output /tmp/ens.zarr
```

---
name: provenance
description: "Read the processing history (weather_skills_history) recorded on a weather-skills Zarr or plot image (PNG, JPEG, HTML) and print it as a readable lineage, raw JSON, or a bash script that regenerates the file. Can also validate that history against its schema. Use to answer how a file was made and how to reproduce it."
license: MIT
compatibility: Requires Python 3.12 and uv. Inspects a zarr directory or a .png/.jpg/.html figure; reads no credentials and writes nothing.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/provenance.py *)
metadata:
  version: "0.0.4"
  catalog-group: agent-tooling
---

# provenance

Prints the chain of skill runs that produced a Zarr or figure; it reads only and writes nothing.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/provenance.py -i ARTIFACT [--format human|json|script] [--check]
```

### Arguments
- `--input`, `-i` — a Zarr directory, or a `.png`, `.jpg`/`.jpeg`, or `.html`/`.htm` figure. Required.
- `--format` — `human` (readable lineage), `json` (raw history), or `script` (bash reproduction). Default: `human`.
- `--check` — validate the history's schema instead of printing it; overrides `--format`.

### Output
Everything goes to stdout; redirect to keep a file.
- `human`: steps oldest first, each with skill, version, commit (when recorded, plus `dirty` if the working tree had changes), input file name, and the args that were set. Steps that combine several inputs (`concat`, `difference`, `verify`, …) list each input's own history under a label `a`, `b`, …. A Zarr's `weather_skills_source` attr prints first.
- `json`: the history array; a PNG with several labelled history keys prints a `{label: history}` object.
- `script`: a bash script, run from an empty directory, that reruns each step at its recorded commit and writes intermediates as `stepN.zarr` (one file per input branch as `a.zarr`, `b.zarr`, …) and the final step under the artifact's own name.
- `--check`: `valid …` and exit 0; no history exits 1; invalid history lists each violation with its location and exits 2.

### Notes
- An artifact with no history, or one whose history is not a JSON array, prints `no provenance recorded on …` and exits 0 (the latter with a warning on stderr). A missing path or unsupported file type exits 2.
- Review a `script` before running it. Path arguments (`--geojson`, spec files) keep the path as first typed, `--no-<flag>` options are dropped, and a step whose input came from outside the history reads `<UPSTREAM>`, which you replace. Fetch steps read credentials from the environment; none are written into the script.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/provenance.py -i chirps_kenya.zarr --format script > repro.sh
```
Writes a bash script that reruns the fetch and clip steps that made `chirps_kenya.zarr`.

## Details

Where the history is stored, the schema `--check` enforces, and how the reproduction script maps recorded args back to flags: [references/REFERENCE.md](references/REFERENCE.md).

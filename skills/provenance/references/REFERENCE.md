# provenance reference

## Where the history lives

- Zarr: the `weather_skills_history` store attribute (and an optional `weather_skills_source`).
- PNG: the `weather_skills_history` tEXt chunk, plus any `weather_skills_history_<label>` chunks (each read as a separate labelled branch).
- JPEG: the EXIF UserComment.
- HTML: `<meta name="weather_skills_history" content="…">`.

## Schema checked by `--check`

Each history value must be a JSON array. Each entry is an object with:

- `skill` — non-empty string (any name; external tools may write their own).
- `version` — string.
- `args` — object.
- `input` — `null` (a fetcher), a `{basename, hash}` object, or an array of them. Each may carry a nested `history`, which is validated recursively.
- `commit` — optional non-empty string (git SHA).
- `repo` — optional non-empty string (clone URL).
- `dirty` — optional boolean.

Unknown keys are listed as notes and do not fail validation.

## Reproduction script

- A step from the weather-skills repo (or a fork with the same name) runs as `uvx --from git+<repo>@<commit> forecasting-skills <skill> …`. With no recorded commit it runs from `dev`, which may have moved since.
- A step from another repo is cloned once per commit into `./_checkouts` and its script run with `uv run --script`.
- Recorded args are argparse names (`time_dim`) and become flags (`--time-dim`). `True` becomes a bare flag; `False` and `None` are dropped; lists repeat the flag; a list of lists gives one flag with several values; objects are passed as JSON.
- Inputs thread from the previous step's output. Steps with several input branches rebuild each branch first. `verify` gets `--forecast`/`--obs`, `standardize-anomaly` `--input`/`--climatology`, `point-value` `--input`/`--points`; other multi-input skills get repeated `--input`. A PNG with `forecast`/`mclimate` branches passes them as `--forecast`/`--mclimate`.
- Limits of the record: a file passed twice (`-i x -i x`) is recorded once; relative paths are relative to the original working directory; a `dirty: true` step gets a warning comment because its commit may not match what ran.

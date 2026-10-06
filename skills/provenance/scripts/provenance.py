# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "xarray",
#   "zarr",
#   "pillow",
# ]
# ///
"""Inspect weather_skills_history on a Zarr or plot PNG (stdout only; never writes)."""

import json
import shlex
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.provenance import (
    HISTORY_ATTR,
    SOURCE_ATTR,
    coerce_chain,
    parse_chain,
    validate_chain,
)

DEFAULT_REPO = "https://github.com/rhiza-research/weather-skills"
DEFAULT_CLI = "forecasting-skills"
# Branch a step runs from when it recorded no commit; `forecasting-skills` exists there.
DEFAULT_REF = "dev"
PLOTTING_REPO = "https://github.com/rhiza-research/weather-skills-plotting"
PLOTTING_SKILLS = {"plot", "plot-mediogram", "plot-timeseries", "plot-verify"}

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _parent_histories(step: dict) -> list[tuple[str, list]]:
    """``(basename, nested history)`` for every parent that recorded a subgraph."""
    value = step.get("input")
    items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        history = item.get("history")
        if isinstance(history, list):
            out.append((item.get("basename") or "?", history))
    return out


def _load_zarr(path: Path) -> dict:
    import xarray as xr

    try:
        with xr.open_zarr(path, consolidated=False) as ds:
            attrs = dict(ds.attrs)
    except Exception as exc:  # noqa: BLE001
        raise UsageError(
            f"Error: could not open {path} as a zarr store: {exc}", prefix=False
        ) from None
    chains = {}
    raw = attrs.get(HISTORY_ATTR)
    coerced = coerce_chain(raw, path.name) if raw else None
    if coerced is not None:
        chains[path.name] = coerced
    return {"chains": chains, "source": attrs.get(SOURCE_ATTR), "name": path.name}


def _load_png(path: Path) -> dict:
    from PIL import Image

    try:
        with Image.open(path) as img:
            info = dict(img.info)
    except Exception as exc:  # noqa: BLE001
        raise UsageError(f"Error: could not open {path} as a PNG: {exc}", prefix=False) from None
    chains = {}
    for key in sorted(info):
        if key == HISTORY_ATTR:
            slot = path.name
        elif key.startswith(f"{HISTORY_ATTR}_"):
            slot = key[len(f"{HISTORY_ATTR}_") :]
        else:
            continue
        if info[key] and slot not in chains:
            coerced = coerce_chain(info[key], f"{path.name} ({key})")
            if coerced is not None:
                chains[slot] = coerced
    return {"chains": chains, "source": None, "name": path.name}


def _read_artifact(path: Path) -> dict:
    if not path.exists():
        raise UsageError(f"Error: {path} not found.", prefix=False)
    if path.is_dir():
        return _load_zarr(path)
    if path.is_file() and path.suffix.lower() == ".png":
        return _load_png(path)
    raise UsageError(
        f"Error: {path} is neither a zarr directory nor a .png file; cannot inspect provenance.",
        prefix=False,
    )


def _read_raw_histories(path: Path) -> dict:
    """Return {location_key: raw_string} for every weather_skills_history value present."""
    if not path.exists():
        raise UsageError(f"Error: {path} not found.", prefix=False)
    if path.is_dir():
        import xarray as xr

        try:
            with xr.open_zarr(path, consolidated=False) as ds:
                attrs = dict(ds.attrs)
        except Exception as exc:  # noqa: BLE001
            raise UsageError(
                f"Error: could not open {path} as a zarr store: {exc}", prefix=False
            ) from None
        return {HISTORY_ATTR: attrs[HISTORY_ATTR]} if attrs.get(HISTORY_ATTR) else {}
    if path.is_file() and path.suffix.lower() == ".png":
        from PIL import Image

        try:
            with Image.open(path) as img:
                info = dict(img.info)
        except Exception as exc:  # noqa: BLE001
            raise UsageError(
                f"Error: could not open {path} as a PNG: {exc}", prefix=False
            ) from None
        raw = {}
        for key in sorted(info):
            if (key == HISTORY_ATTR or key.startswith(f"{HISTORY_ATTR}_")) and info[key]:
                raw[key] = info[key]
        return raw
    raise UsageError(
        f"Error: {path} is neither a zarr directory nor a .png file; cannot inspect provenance.",
        prefix=False,
    )


def _run_check(path: Path) -> tuple[int, str]:
    """Validate schema: 0=valid, 1=absent, 2=invalid."""
    raw_histories = _read_raw_histories(path)
    if not raw_histories:
        return 1, f"no provenance found on {path}"

    violations, notes = [], []
    for key, raw in raw_histories.items():
        try:
            chain = parse_chain(raw)
        except ValueError as exc:
            violations.append(f"{key}: {exc}")
            continue
        chain_violations, chain_notes = validate_chain(chain, key)
        violations.extend(chain_violations)
        notes.extend(chain_notes)

    if violations:
        lines = [f"invalid weather_skills_history on {path}:"]
        lines += [f"  - {v}" for v in violations]
        if notes:
            lines.append("notes (not failures):")
            lines += [f"  - {n}" for n in notes]
        return 2, "\n".join(lines)

    lines = [f"valid weather_skills_history on {path}"]
    if notes:
        lines.append("notes (not failures):")
        lines += [f"  - {n}" for n in notes]
    return 0, "\n".join(lines)


def _print_step(n: int, step: dict, indent: str) -> None:
    if not isinstance(step, dict):
        print(f"{indent}{n}. (malformed entry: not an object)")
        return
    identity = f"{step.get('skill', '?')} (v{step.get('version', '?')}"
    commit = step.get("commit")
    if isinstance(commit, str) and commit:
        short = commit[:12] if len(commit) > 12 else commit
        identity += f" @{short}"
        if step.get("dirty") is True:
            identity += " dirty"
    identity += ")"
    print(f"{indent}{n}. {identity}")
    step_input = step.get("input")
    if step_input is None:
        inp = "(none -- fetcher)"
    elif isinstance(step_input, list):
        inp = "[" + ", ".join(i.get("basename", "?") for i in step_input) + "]"
    elif isinstance(step_input, dict):
        inp = step_input.get("basename", "?")
    else:
        inp = str(step_input)
    print(f"{indent}   input: {inp}")
    args = step.get("args") or {}
    if args:
        print(f"{indent}   args: " + ", ".join(f"{k}={v!r}" for k, v in sorted(args.items())))
    else:
        print(f"{indent}   args: (none)")


def _print_chain(chain: list, indent: str) -> None:
    for n, step in enumerate(chain, start=1):
        _print_step(n, step, indent)
        if not isinstance(step, dict):
            continue
        branches = _parent_histories(step)
        if len(branches) < 2:
            continue
        for idx, (basename, history) in enumerate(branches):
            label = chr(ord("a") + idx) if idx < 26 else str(idx)
            print(f"{indent}   input branch {label} ({basename}):")
            if not history:
                print(f"{indent}     (no recorded history)")
                continue
            _print_chain(history, indent + "     ")


def _render_human(data: dict) -> None:
    chains = data["chains"]
    source = data.get("source")
    if source:
        print(f"weather_skills_source: {source}")
        print()
    multi = len(chains) > 1
    first = True
    for label, chain in chains.items():
        if not first:
            print()
        first = False
        if multi:
            print(f"branch {label}:")
        indent = "  " if multi else ""
        _print_chain(chain, indent)


class _ScriptState:
    """Shared state while emitting one reproduction script."""

    def __init__(self):
        # Canonical JSON of a history prefix -> the file that prefix produces,
        # so a subgraph shared by several branches is reproduced only once.
        self.memo: dict[str, str] = {}
        # (repo, ref) -> shell variable holding a local checkout of that repo.
        self.checkouts: dict[tuple[str, str], str] = {}
        self.unpinned: set[str] = set()


def _canonical(chain: list) -> str:
    return json.dumps(chain, sort_keys=True, ensure_ascii=False)


def _repo_and_ref(step: dict, state: _ScriptState) -> tuple[str, str]:
    skill = step.get("skill", "?")
    repo = step.get("repo") if isinstance(step.get("repo"), str) and step["repo"] else None
    if repo is None:
        repo = PLOTTING_REPO if skill in PLOTTING_SKILLS else DEFAULT_REPO
    commit = step.get("commit")
    if isinstance(commit, str) and commit:
        return repo, commit
    state.unpinned.add(skill)
    return repo, DEFAULT_REF


def _runner(step: dict, state: _ScriptState) -> tuple[list[str], list[str]]:
    """``(setup lines, command prefix)`` that invoke ``step``'s skill at its revision.

    Skills in the main repo run through its ``forecasting-skills`` CLI. Plotting
    skills have no CLI and resolve their package by relative path, so they run
    from a local checkout of their repo.
    """
    skill = step.get("skill", "?")
    repo, ref = _repo_and_ref(step, state)
    if repo.rstrip("/").removesuffix(".git").endswith("/" + PLOTTING_REPO.rsplit("/", 1)[-1]):
        setup = []
        var = state.checkouts.get((repo, ref))
        if var is None:
            var = f"CHECKOUT_{len(state.checkouts) + 1}"
            state.checkouts[(repo, ref)] = var
            setup.append(f'{var}="$(_checkout {shlex.quote(repo)} {shlex.quote(ref)})"')
        script = f"skills/{skill}/scripts/{skill.replace('-', '_')}.py"
        return setup, ["uv", "run", f'"${var}/{script}"']
    return [], ["uvx", "--from", f"git+{repo}@{ref}", DEFAULT_CLI, skill]


def _arg_text(value) -> str:
    # A plot spec (or any structured arg) must reach the CLI as JSON, not a Python repr.
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _command(step: dict, inputs: list, output: str, state: _ScriptState) -> list[str]:
    args = step.get("args") or {}
    setup, parts = _runner(step, state)
    for dest, value in sorted(args.items()):
        flag = "--" + dest.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            parts.append(flag)
        elif isinstance(value, list) and not any(isinstance(v, (dict, list)) for v in value):
            for item in value:
                parts += [flag, shlex.quote(str(item))]
        else:
            parts += [flag, shlex.quote(_arg_text(value))]
    for flag, path in inputs:
        parts += [flag, shlex.quote(path)]
    parts += ["--output", shlex.quote(output)]
    lines = list(setup)
    if step.get("dirty") is True:
        lines.append(
            f"# dirty working tree when this step ran; commit {step.get('commit')} "
            "may not match what executed"
        )
    lines.append(" ".join(parts))
    return lines


def _join_inputs(branch_outputs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    labels = [lab for lab, _ in branch_outputs]
    if set(labels) <= {"forecast", "mclimate"}:
        order = {"forecast": 0, "mclimate": 1}
        ordered = sorted(branch_outputs, key=lambda x: order.get(x[0], 99))
        return [("--" + lab, out) for lab, out in ordered]
    return [("--input", out) for _, out in branch_outputs]


def _emit_chain(
    chain: list, prefix: str, final_output: str, state: _ScriptState
) -> tuple[list[str], str | None]:
    """Emit commands for ``chain``; return ``(lines, path of its final output)``.

    Any step that is a join recurses into its parent subgraphs. A prefix that
    an earlier branch already produced is reused instead of re-run, so the
    returned path may be that earlier file rather than ``final_output``.
    """
    lines: list[str] = []
    prev = None
    n = len(chain)
    for i, step in enumerate(chain, start=1):
        key = _canonical(chain[:i])
        if key in state.memo:
            prev = state.memo[key]
            continue
        if not isinstance(step, dict):
            lines.append(f"# (malformed entry skipped: not an object) -- step {i}")
            continue
        skill = step.get("skill", "?")
        step_input = step.get("input")
        output = final_output if i == n else f"{prefix}{i}.zarr"
        branches = _parent_histories(step)

        if len(branches) >= 2:
            branch_outputs = []
            for idx, (_basename, history) in enumerate(branches):
                label = chr(ord("a") + idx) if idx < 26 else str(idx)
                branch_out = f"{prefix}{label}.zarr" if prefix != "step" else f"{label}.zarr"
                lines.append(f"# --- input branch {label} ---")
                if history:
                    sub, out = _emit_chain(history, f"{prefix}{label}_", branch_out, state)
                    lines += sub or [f"# same as an earlier branch: reuses {out}"]
                else:
                    lines.append(
                        f"# branch {label} records no steps; supply {branch_out} yourself."
                    )
                    out = branch_out
                branch_outputs.append((label, out))
                lines.append("")
            lines.append("# --- combine into the final step ---")
            inputs = _join_inputs(branch_outputs)
        elif isinstance(step_input, list):
            inputs = []
            for j, item in enumerate(step_input):
                if j == 0 and prev is not None:
                    inputs.append(("--input", prev))
                else:
                    inputs.append(("--input", item.get("basename", "?")))
        elif step_input is None:
            inputs = []
        elif prev is not None:
            inputs = [("--input", prev)]
        else:
            lines.append(f"# step {i} ({skill})'s input is an artifact outside this chain;")
            lines.append("# reproduce it separately and replace <UPSTREAM> below.")
            inputs = [("--input", "<UPSTREAM>")]

        lines += _command(step, inputs, output, state)
        state.memo[key] = output
        prev = output
    return lines, prev


def _preamble(state: _ScriptState) -> list[str]:
    lines = [
        "#!/usr/bin/env bash",
        "# Reproduction script generated by the `provenance` skill.",
        "set -eo pipefail",
    ]
    if state.unpinned:
        lines.append(
            f"# No commit was recorded for: {', '.join(sorted(state.unpinned))}. "
            f"Those steps run from the `{DEFAULT_REF}` branch, which may have moved "
            "since the artifact was made."
        )
    if state.checkouts:
        lines += [
            "",
            "# Plotting skills run from a local checkout of their repo.",
            "_checkout() {",
            '  local dir; dir="$(mktemp -d)"',
            '  git clone -q "$1" "$dir" && git -C "$dir" checkout -q "$2" && echo "$dir"',
            "}",
        ]
    return lines + [""]


def _render_script(data: dict) -> None:
    chains = data["chains"]
    name = data.get("name", "artifact")
    state = _ScriptState()
    lines: list[str] = []

    if len(chains) <= 1:
        chain = next(iter(chains.values()))
        lines, _ = _emit_chain(chain, prefix="step", final_output=name, state=state)
        print("\n".join(_preamble(state) + lines))
        return

    terminal = None
    branch_outputs = []
    for label, chain in chains.items():
        if not chain:
            continue
        terminal = chain[-1]
        sub = chain[:-1]
        branch_out = f"{label}.zarr"
        lines.append(f"# --- input branch {label} ---")
        if sub:
            sub_lines, out = _emit_chain(sub, f"{label}_", branch_out, state)
            lines += sub_lines or [f"# same as an earlier branch: reuses {out}"]
        else:
            lines.append(
                f"# branch {label} records no steps before the final step; "
                f"supply {branch_out} yourself."
            )
            out = branch_out
        branch_outputs.append((label, out))
        lines.append("")

    lines.append("# --- combine into the final step ---")
    if terminal is not None:
        if not isinstance(terminal, dict):
            lines.append("# (malformed terminal step skipped: not an object)")
        else:
            labels = [lab for lab, _ in branch_outputs]
            if not set(labels) <= {"forecast", "mclimate"}:
                branch_outputs = sorted(branch_outputs, key=lambda x: x[0])
            lines += _command(terminal, _join_inputs(branch_outputs), name, state)
    print("\n".join(_preamble(state) + lines))


@weather_skill(
    name="provenance",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "-i",
    "--input",
    required=True,
    help="Artifact to inspect: a zarr dir or a .png file.",
)
@weather_skill.argument(
    "--format",
    choices=["human", "json", "script"],
    default="human",
    help="Output view: human-readable lineage, raw JSON chain, or a reproduction script.",
)
@weather_skill.argument(
    "--check",
    action="store_true",
    help="Validate weather_skills_history schema (exit 0/1/2).",
)
def provenance(input, format, check, **kwargs):
    """Inspect weather_skills_history on a Zarr or plot PNG (stdout only; never writes)."""
    if check:
        code, report = _run_check(Path(input))
        if code == 0:
            print(report)
            return
        if code == 1:
            raise DataError(report, prefix=False)
        raise UsageError(report, prefix=False)

    data = _read_artifact(Path(input))
    if not data["chains"]:
        print(f"no provenance recorded on {input}")
        return

    if format == "human":
        _render_human(data)
    elif format == "json":
        chains = data["chains"]
        payload = next(iter(chains.values())) if len(chains) == 1 else chains
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _render_script(data)


if __name__ == "__main__":
    provenance()

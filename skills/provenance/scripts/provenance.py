# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pillow",
# ]
# ///
"""Print the processing history recorded on a Zarr or plot image as a lineage, JSON, or a script that regenerates it."""

import html
import json
import re
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
# Ref a step runs from when it recorded no commit: the CLI above lives on dev
# (the default branch names its CLI differently).
DEFAULT_REF = "dev"
# Joins whose inputs are not a repeated --input, in the order the decorator
# records them (declaration order).
JOIN_FLAGS = {
    "verify": ["--forecast", "--obs"],
    "standardize-anomaly": ["--input", "--climatology"],
    "point-value": ["--input", "--points"],
}
_EXIF_USER_COMMENT = 0x9286
_HTML_META_RE = re.compile(
    rf'<meta\s+name=["\']{HISTORY_ATTR}["\']\s+content=["\'](.*?)["\']\s*/?>',
    re.IGNORECASE | re.DOTALL,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"


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


def _read_raw_histories(path: Path) -> tuple[dict, str | None]:
    """``({location_key: raw_string}, weather_skills_source)`` for every history present.

    A zarr keeps one store attr; a PNG keeps tEXt keys (``weather_skills_history``
    and, for older two-input plots, ``weather_skills_history_<label>``); a JPEG
    keeps the EXIF UserComment; an HTML figure keeps a ``<meta>`` tag.
    """
    if not path.exists():
        raise UsageError(f"Error: {path} not found.", prefix=False)
    suffix = path.suffix.lower()
    if path.is_dir():
        import xarray as xr

        try:
            with xr.open_zarr(path, consolidated=False) as ds:
                attrs = dict(ds.attrs)
        except Exception as exc:  # noqa: BLE001
            raise UsageError(
                f"Error: could not open {path} as a zarr store: {exc}", prefix=False
            ) from None
        raw = {HISTORY_ATTR: attrs[HISTORY_ATTR]} if attrs.get(HISTORY_ATTR) else {}
        return raw, attrs.get(SOURCE_ATTR)
    if path.is_file() and suffix in (".png", ".jpg", ".jpeg"):
        from PIL import Image

        try:
            with Image.open(path) as img:
                info = dict(img.info)
                comment = img.getexif().get(_EXIF_USER_COMMENT)
        except Exception as exc:  # noqa: BLE001
            raise UsageError(
                f"Error: could not open {path} as an image: {exc}", prefix=False
            ) from None
        if suffix != ".png":
            if isinstance(comment, bytes):
                comment = comment.removeprefix(b"ASCII\x00\x00\x00").decode("utf-8", "replace")
            return ({HISTORY_ATTR: comment} if comment else {}), None
        raw = {}
        for key in sorted(info):
            if (key == HISTORY_ATTR or key.startswith(f"{HISTORY_ATTR}_")) and info[key]:
                raw[key] = info[key]
        return raw, None
    if path.is_file() and suffix in (".html", ".htm"):
        match = _HTML_META_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        return ({HISTORY_ATTR: html.unescape(match.group(1))} if match else {}), None
    raise UsageError(
        f"Error: {path} is neither a zarr directory nor a .png/.jpg/.html figure; "
        "cannot inspect provenance.",
        prefix=False,
    )


def _read_artifact(path: Path) -> dict:
    raw, source = _read_raw_histories(path)
    chains = {}
    for key, value in raw.items():
        slot = path.name if key == HISTORY_ATTR else key[len(f"{HISTORY_ATTR}_") :]
        coerced = coerce_chain(value, f"{path.name} ({key})")
        if coerced is not None and slot not in chains:
            chains[slot] = coerced
    return {"chains": chains, "source": source, "name": path.name}


def _run_check(path: Path) -> tuple[int, str]:
    """Validate schema: 0=valid, 1=absent, 2=invalid."""
    raw_histories, _source = _read_raw_histories(path)
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
        names = [i.get("basename", "?") if isinstance(i, dict) else "?" for i in step_input]
        inp = "[" + ", ".join(names) + "]"
    elif isinstance(step_input, dict):
        inp = step_input.get("basename", "?")
    else:
        inp = str(step_input)
    print(f"{indent}   input: {inp}")
    args = step.get("args") or {}
    if not isinstance(args, dict):
        print(f"{indent}   args: (malformed: not an object)")
        return
    # None means the flag was not passed; leave it out.
    shown = [f"{k}={v!r}" for k, v in sorted(args.items()) if v is not None]
    print(f"{indent}   args: " + (", ".join(shown) or "(none)"))


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


def _command(step: dict, inputs: list, output: str) -> str:
    skill = step.get("skill", "?")
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    repo = step.get("repo") if isinstance(step.get("repo"), str) and step["repo"] else DEFAULT_REPO
    commit = step.get("commit")
    ref = commit if isinstance(commit, str) and commit else DEFAULT_REF
    # weather-skills (or a fork) has the CLI. Another repo may name its CLI
    # differently or have none: run the skill's one script from a checkout, as
    # skills-runner does.
    if repo.rsplit("/", 1)[-1] == DEFAULT_REPO.rsplit("/", 1)[-1]:
        parts = ["uvx", "--from", f"git+{repo}@{ref}", DEFAULT_CLI, skill]
    else:
        checkout = f'"$(_checkout {shlex.quote(repo)} {shlex.quote(ref)})"'
        parts = ["uv", "run", "--script", f"{checkout}/skills/{skill}/scripts/*.py"]
    for dest, value in sorted(args.items()):
        flag = "--" + dest.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            parts.append(flag)
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, list):
                # append + nargs="+" (`-v tp t2m`): one flag, several values.
                parts += [flag] + [shlex.quote(str(v)) for v in item]
            elif isinstance(item, dict):
                # A structured arg (a JSON spec) goes back as JSON, not a repr.
                parts += [flag, shlex.quote(json.dumps(item, sort_keys=True))]
            else:
                parts += [flag, shlex.quote(str(item))]
    for flag, path in inputs:
        parts += [flag, shlex.quote(path)]
    parts += ["--output", shlex.quote(output)]
    line = " ".join(parts)
    if step.get("dirty") is True:
        return (
            f"# dirty working tree when this step ran; commit {step.get('commit')} "
            f"may not match what executed\n{line}"
        )
    return line


def _emit_graph(chain: list, prefix: str, final_output: str) -> list:
    """Emit one command per step; a join step first reproduces each parent subgraph.

    A join can sit anywhere on the path (its output feeding later steps), so every
    step is checked, and nested joins recurse.
    """
    lines = []
    prev = None
    n = len(chain)
    for i, step in enumerate(chain, start=1):
        if not isinstance(step, dict):
            lines.append(f"# (malformed entry skipped: not an object) -- step {i}")
            continue
        skill = step.get("skill", "?")
        step_input = step.get("input")
        output = final_output if i == n else f"{prefix}{i}.zarr"
        flags = JOIN_FLAGS.get(skill, [])
        branches = _parent_histories(step)

        if len(branches) >= 2:
            branch_outputs = []
            for idx, (basename, history) in enumerate(branches):
                label = chr(ord("a") + idx) if idx < 26 else str(idx)
                branch_out = f"{prefix}{label}.zarr" if prefix != "step" else f"{label}.zarr"
                lines.append(f"# --- input branch {label} ---")
                if history:
                    lines += _emit_graph(history, f"{prefix}{label}_", branch_out)
                else:
                    lines.append(
                        f"# branch {label} records no steps; "
                        f"supply {basename} as {branch_out} yourself."
                    )
                branch_outputs.append(branch_out)
                lines.append("")
            lines.append(f"# --- combine the input branches ({skill}) ---")
            inputs = [
                (flags[j] if j < len(flags) else "--input", out)
                for j, out in enumerate(branch_outputs)
            ]
        elif isinstance(step_input, list):
            inputs = []
            for j, item in enumerate(step_input):
                flag = flags[j] if j < len(flags) else "--input"
                if j == 0 and prev is not None:
                    inputs.append((flag, prev))
                else:
                    name = item.get("basename", "?") if isinstance(item, dict) else "?"
                    inputs.append((flag, name))
        elif step_input is None:
            inputs = []
        elif prev is not None:
            inputs = [("--input", prev)]
        else:
            lines.append(f"# step {i} ({skill})'s input is an artifact outside this chain;")
            lines.append("# reproduce it separately and replace <UPSTREAM> below.")
            inputs = [("--input", "<UPSTREAM>")]

        lines.append(_command(step, inputs, output))
        prev = output
    return lines


def _with_checkout_helper(lines: list) -> list:
    """Define ``_checkout`` after the header when a step runs from a repo checkout."""
    if not any("$(_checkout " in line for line in lines):
        return lines
    helper = [
        "# Clone each repo once per ref into ./_checkouts and print its path.",
        "_checkout() {",
        '  local dir="_checkouts/${1##*/}@$2"',
        '  if [ ! -d "$dir" ]; then',
        '    git clone -q "$1" "$dir" && git -C "$dir" checkout -q "$2"',
        "  fi",
        '  echo "$dir"',
        "}",
        "",
    ]
    return lines[:4] + helper + lines[4:]


def _render_script(data: dict) -> None:
    chains = data["chains"]
    name = data.get("name", "artifact")
    lines = [
        "#!/usr/bin/env bash",
        "# Reproduction script generated by the `provenance` skill.",
        "set -eo pipefail",
        "",
    ]

    if len(chains) <= 1:
        chain = next(iter(chains.values()))
        lines += _emit_graph(chain, prefix="step", final_output=name)
        print("\n".join(_with_checkout_helper(lines)))
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
            lines += _emit_graph(sub, prefix=f"{label}_", final_output=branch_out)
        else:
            lines.append(
                f"# branch {label} records no steps before the final step; "
                f"supply {branch_out} yourself."
            )
        branch_outputs.append((label, branch_out))
        lines.append("")

    lines.append("# --- combine into the final step ---")
    if terminal is not None:
        if not isinstance(terminal, dict):
            lines.append("# (malformed terminal step skipped: not an object)")
        else:
            labels = [lab for lab, _ in branch_outputs]
            if set(labels) <= {"forecast", "mclimate"}:
                order = {"forecast": 0, "mclimate": 1}
                ordered = sorted(branch_outputs, key=lambda x: order.get(x[0], 99))
                ins = [("--" + lab, out) for lab, out in ordered]
            else:
                ordered = sorted(branch_outputs, key=lambda x: x[0])
                ins = [("--input", out) for _, out in ordered]
            lines.append(_command(terminal, ins, name))
    print("\n".join(_with_checkout_helper(lines)))


@weather_skill(
    name="provenance",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "-i",
    "--input",
    required=True,
    help="Artifact to inspect: a zarr dir or a .png, .jpg, or .html figure.",
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
    """Print the processing history recorded on a Zarr or plot image as a lineage, JSON, or a script that regenerates it."""
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

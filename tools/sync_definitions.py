# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Copy the canonical onset-definition registry into every skill that consumes it.

Skills are self-contained directories (run via uvx, installed singly), so they cannot
import `registry/`. A consuming skill ships a BYTE-IDENTICAL copy of
`registry/onset_definitions.toml` at `skills/<skill>/references/onset_definitions.toml`.
This tool writes those copies, and with `--check` fails when one has drifted.

Line endings are normalised to LF both when copying and when comparing, so a Windows
checkout with core.autocrlf does not register as drift.

A consumer listed in CONSUMERS whose skill directory does not exist on the current
branch is skipped with a note (a consumer can be listed before its skill lands).

Usage:
    uv run tools/sync_definitions.py            # write every consumer copy
    uv run tools/sync_definitions.py --check    # exit 1 if a copy differs or is missing
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANONICAL = REPO / "registry" / "onset_definitions.toml"
SKILLS_DIR = REPO / "skills"

# Skills that ship a copy of the registry. Add a skill here when it starts reading it.
CONSUMERS = ("indicator", "onset-date")

COPY_RELPATH = Path("references") / "onset_definitions.toml"


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _resolve(canonical, skills_dir, consumers):
    """Defaults resolve at call time, so the module-level paths can be patched in tests."""
    return (
        CANONICAL if canonical is None else canonical,
        SKILLS_DIR if skills_dir is None else skills_dir,
        CONSUMERS if consumers is None else consumers,
    )


def consumer_copy(skill: str, skills_dir: Path = SKILLS_DIR) -> Path:
    return skills_dir / skill / COPY_RELPATH


def sync(
    canonical: Path | None = None,
    skills_dir: Path | None = None,
    consumers: tuple[str, ...] | None = None,
) -> list[str]:
    """Write every present consumer's copy; return one human-readable line per consumer."""
    canonical, skills_dir, consumers = _resolve(canonical, skills_dir, consumers)
    body = _lf(canonical.read_bytes())
    lines = []
    for skill in consumers:
        if not (skills_dir / skill).is_dir():
            lines.append(f"skip {skill}: no skills/{skill}/ on this branch")
            continue
        dest = consumer_copy(skill, skills_dir)
        if dest.is_file() and _lf(dest.read_bytes()) == body:
            lines.append(f"ok   {skill}: up to date")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        lines.append(f"wrote {dest.relative_to(skills_dir.parent).as_posix()}")
    return lines


def check(
    canonical: Path | None = None,
    skills_dir: Path | None = None,
    consumers: tuple[str, ...] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (problems, notes). A problem is a missing or differing copy of a present skill."""
    canonical, skills_dir, consumers = _resolve(canonical, skills_dir, consumers)
    body = _lf(canonical.read_bytes())
    problems, notes = [], []
    for skill in consumers:
        if not (skills_dir / skill).is_dir():
            notes.append(f"skip {skill}: no skills/{skill}/ on this branch")
            continue
        dest = consumer_copy(skill, skills_dir)
        rel = dest.relative_to(skills_dir.parent).as_posix()
        if not dest.is_file():
            problems.append(f"{rel}: missing")
        elif _lf(dest.read_bytes()) != body:
            problems.append(f"{rel}: differs from registry/onset_definitions.toml")
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["--check"]):
        print("Usage: sync_definitions.py [--check]", file=sys.stderr)
        return 2
    if args == ["--check"]:
        problems, notes = check()
        for note in notes:
            print(note)
        if problems:
            print("Onset-definition copies out of sync (run: python tools/sync_definitions.py):")
            for p in problems:
                print(f"  {p}")
            return 1
        return 0
    for line in sync():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

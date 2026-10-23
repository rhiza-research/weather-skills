"""Build hook: ship skills/ without the per-skill tests.

skills-runner force-includes the whole skills/ tree, which hatch's `exclude`
can't trim. Per-skill tests import the root tests/conftest.py, which isn't
shipped, so they can't run from an install.
"""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        force = build_data["force_include"]
        force.pop("skills", None)
        root = Path(self.root)
        for path in sorted((root / "skills").rglob("*")):
            rel = path.relative_to(root)
            if path.is_file() and rel.parts[2:3] != ("tests",):
                force[str(path)] = rel.as_posix()

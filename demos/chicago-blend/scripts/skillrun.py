"""Call weather-skills scripts in-process: same code as `uv run SCRIPT ...`, without a new process per call.

Run the analysis scripts with the weather-skills repo environment
(`uv run --project $SKILLS/.. python -I ...`), which has every skill's dependencies.
"""

import contextlib
import importlib.util
import io
import os
from pathlib import Path

SKILLS = Path(os.environ["SKILLS"])
_loaded = {}


def skill(name, stem):
    """The decorated entry point of skills/NAME/scripts/STEM.py."""
    key = (name, stem)
    if key not in _loaded:
        path = SKILLS / name / "scripts" / f"{stem}.py"
        spec = importlib.util.spec_from_file_location(f"skill_{name}_{stem}".replace("-", "_"), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The entry point is whatever the script calls under `if __name__ == "__main__":`.
        main = path.read_text().split('if __name__ == "__main__":')[1].split("(")[0].strip()
        _loaded[key] = getattr(module, main)
    return _loaded[key]


def run(name, stem, *argv, quiet=True):
    """Run one skill with CLI arguments; raise if it exits non-zero."""
    out, err = io.StringIO(), io.StringIO()
    redirect = (contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)) if quiet else ()
    with contextlib.ExitStack() as stack:
        for r in redirect:
            stack.enter_context(r)
        try:
            skill(name, stem)([str(a) for a in argv])
        except SystemExit as exc:
            if exc.code:
                raise RuntimeError(
                    f"{name} {' '.join(map(str, argv))}\n{err.getvalue()[-2000:]}"
                ) from None
    return err.getvalue()

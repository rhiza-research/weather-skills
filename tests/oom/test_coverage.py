"""Every skill that handles gridded or tabular data has OOM cases (or a stated reason not to)."""

from __future__ import annotations

from oom_cases_fetchers import CASES as FETCH_CASES
from oom_cases_transforms import CASES as TRANSFORM_CASES
from oom_harness import REPO_ROOT, declares_dask

# Skills with no dataset to stream, and fetchers whose cases are still to come.
NO_DATA = {"provenance", "resolve-region", "resolve-time", "submit-feedback"}
FETCHERS_PENDING = {
    "3dpaws-fetch": "station data; cases come after the gridded fetchers",
    "ghcn-daily-fetch": "station data; cases come after the gridded fetchers",
    "openaq-fetch": "station data; cases come after the gridded fetchers",
    "tahmo-fetch": "station data; cases come after the gridded fetchers",
}


def _skills() -> set[str]:
    return {p.name for p in (REPO_ROOT / "skills").iterdir() if (p / "scripts").is_dir()}


def test_every_skill_has_cases():
    covered = {c.skill for c in TRANSFORM_CASES} | {c.skill for c in FETCH_CASES}
    missing = sorted(_skills() - covered - NO_DATA - set(FETCHERS_PENDING))
    assert not missing, f"skills with no OOM case in tests/oom/oom_cases_*.py: {missing}"


def test_exemptions_are_current():
    stale = sorted((NO_DATA | set(FETCHERS_PENDING)) - _skills())
    assert not stale, f"exempted skills that no longer exist: {stale}"


def test_dask_skills_have_cases():
    covered = {(c.skill, c.script) for c in [*TRANSFORM_CASES, *FETCH_CASES]}
    declared = {
        (path.parents[1].name, path.name)
        for path in (REPO_ROOT / "skills").glob("*/scripts/*.py")
        if declares_dask(path.parents[1].name, path.name)
    }
    missing = sorted(declared - covered)
    assert not missing, f"install weather-skills-core[dask] but have no OOM case: {missing}"

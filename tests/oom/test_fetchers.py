"""Gridded fetcher skills on real requests, in the subprocess OOM tiers.

Every test here downloads data and needs ``--oom-fetchers``. There is no graph
tier for fetchers yet: it needs each source mocked, which comes as each fetcher
gains its dask path.
"""

from __future__ import annotations

import pytest
from oom_cases_fetchers import CASES
from oom_harness import expect_dask, skill_script
from oom_tiers import Sized, full_size, scaling, smoke

pytestmark = pytest.mark.fetchers


def _require_creds(case):
    missing = case.missing_creds()
    if missing:
        pytest.skip(f"{case.id}: credentials not available: {', '.join(missing)}")


@pytest.mark.scaling
@pytest.mark.parametrize("case", expect_dask(CASES))
def test_scaling(case, oom, tmp_path):
    """Peak memory barely moves between the small and the large request."""
    _require_creds(case)
    small, large = tmp_path / "small.zarr", tmp_path / "large.zarr"
    scaling(
        oom,
        skill_script(case.skill, case.script),
        Sized([*case.small_args, "-o", str(small)], None, small),
        Sized([*case.large_args, "-o", str(large)], None, large),
    )


@pytest.mark.full_size
@pytest.mark.parametrize("case", expect_dask(CASES))
def test_full_size(case, oom, tmp_path):
    """A request several times the cap; the run stays under it."""
    _require_creds(case)
    if not case.full_args:
        pytest.skip(f"{case.id}: no full_args request defined")
    output = tmp_path / "out.zarr"
    full_size(
        oom,
        skill_script(case.skill, case.script),
        [*case.full_args, "-o", str(output)],
        output,
        min_output_bytes=int(0.9 * oom.factor * oom.limit),
    )


@pytest.mark.smoke
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_smoke(case, oom, tmp_path):
    """The small request works against today's code."""
    _require_creds(case)
    output = tmp_path / "out.zarr"
    smoke(oom, skill_script(case.skill, case.script), [*case.small_args, "-o", str(output)], output)

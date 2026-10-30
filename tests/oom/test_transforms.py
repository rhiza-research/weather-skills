"""Transform skills on synthetic data, in every OOM tier.

A case is a strict expected failure until its skill installs
``weather-skills-core[dask]``; from then on it must pass.
"""

from __future__ import annotations

import pytest
from oom_cases_transforms import CASES, transform_argv
from oom_graph import run_in_process
from oom_harness import expect_dask, fmt_bytes, skill_script, stored_chunk_bytes
from oom_synthetic import synthetic_zarr
from oom_tiers import Sized, full_size, remove_output, scaling, smoke

STREAMING = [c for c in CASES if c.streams]

SMOKE_BYTES = 16 * 2**20
PIPELINE_CHUNKS = 8
GRAPH_BYTES = 8 * 2**20
GRAPH_CHUNK_BYTES = 2**20
GRAPH_DASK_ARGS = ["--dask", "--dask-workers", "2", "--dask-chunk-size", "1MiB"]
# A compute before the write may return at most this much (coords, masks, summaries).
SMALL_RESULT_BYTES = 64 * 2**10


def _tail(text: str, n: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


@pytest.mark.graph
@pytest.mark.parametrize("case", expect_dask(STREAMING))
def test_graph(case, oom, dask_in_process, tmp_path, capfd):
    """Lazy until the write: no big computes first, and dask chunks reach the writer."""
    args, input_bytes, output = transform_argv(case, GRAPH_BYTES, oom.cache, tmp_path)
    try:
        code, report = run_in_process(case.skill, case.script, [*args, *GRAPH_DASK_ARGS])
        err = capfd.readouterr().err
        if code:
            pytest.fail(f"exit {code}\n{_tail(err)}", pytrace=False)
        limit = max(SMALL_RESULT_BYTES, input_bytes // 32)
        eager = [c for c in report.pre_write() if c.result_bytes > limit]
        if eager:
            sizes = ", ".join(fmt_bytes(c.result_bytes) for c in eager)
            pytest.fail(
                f"{len(eager)} compute(s) before the write returned more than "
                f"{fmt_bytes(limit)} of {fmt_bytes(input_bytes)} input: {sizes}",
                pytrace=False,
            )
        if not case.output or not case.lazy_output:
            return
        if not report.writes:
            pytest.fail("no Dataset write seen", pytrace=False)
        for write in report.writes:
            if write.eager_vars:
                pytest.fail(
                    f"{write.target} write got numpy (not dask) data for {write.eager_vars}",
                    pytrace=False,
                )
            if write.max_chunk_bytes > 2 * GRAPH_CHUNK_BYTES:
                pytest.fail(
                    f"{write.target} write has a {fmt_bytes(write.max_chunk_bytes)} chunk; "
                    f"--dask-chunk-size is {fmt_bytes(GRAPH_CHUNK_BYTES)}",
                    pytrace=False,
                )
    finally:
        remove_output(output)


@pytest.mark.scaling
@pytest.mark.parametrize("case", expect_dask(STREAMING, memory=True))
def test_scaling(case, oom, tmp_path):
    """Peak memory barely moves between a small and a large input.

    Dask chunks are never smaller than the stored chunks, so the small run gets
    at least ``PIPELINE_CHUNKS`` of them; with fewer, memory is still ramping up
    there even for a skill that streams.
    """
    stored = max(
        stored_chunk_bytes(synthetic_zarr(i.kind, oom.small, oom.cache, **dict(i.variant)))
        for i in case.inputs
    )
    small = max(oom.small, PIPELINE_CHUNKS * stored)
    large = max(oom.large, 5 * small)
    sized = []
    for label, size in (("small", small), ("large", large)):
        tmp = tmp_path / label
        tmp.mkdir()
        sized.append(Sized(*transform_argv(case, size, oom.cache, tmp)))
    scaling(oom, skill_script(case.skill, case.script), *sized)


@pytest.mark.full_size
@pytest.mark.parametrize("case", expect_dask(STREAMING, memory=True))
def test_full_size(case, oom, tmp_path):
    """Inputs several times the cap; the run stays under it."""
    size = int(oom.factor * oom.limit)
    args, _, output = transform_argv(case, size, oom.cache, tmp_path)
    full_size(oom, skill_script(case.skill, case.script), args, output)


@pytest.mark.smoke
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_smoke(case, oom, tmp_path):
    """The case's arguments and inputs work against today's code."""
    args, _, output = transform_argv(case, SMOKE_BYTES, oom.cache, tmp_path)
    smoke(oom, skill_script(case.skill, case.script), args, output)

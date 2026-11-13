"""The subprocess tiers of the OOM suite: smoke, memory scaling, full-size data.

Each takes a skill script plus arguments, runs it with ``oom_harness``, and
fails the test with a readable summary. The in-process graph tier lives in
``oom_graph``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from oom_harness import Run, fmt_bytes, run_skill_capped, zarr_nbytes

# Slack on top of "loaded everything" before a scaling run is killed outright.
CEILING_SLACK = 128 * 2**20
# Ceiling for a fetcher's large run, whose output size is unknown until it ends.
FETCH_CEILING = 2 * 2**30


@dataclass
class Settings:
    small: int  # bytes per synthetic input, small run
    large: int  # bytes per synthetic input, large run
    max_growth: float  # allowed peak growth as a fraction of the data growth
    slack: int  # plus this many bytes, for run-to-run RSS noise
    limit: int  # full-size cap
    factor: float  # full-size inputs = factor x limit
    baseline: bool
    dask_args: list[str]
    timeout: float
    cache: Path
    runs: list[Run] = field(default_factory=list)

    def run(self, script, args, *, limit=None) -> Run:
        run = run_skill_capped(script, args, limit=limit, timeout=self.timeout)
        self.runs.append(run)
        return run


@dataclass(frozen=True)
class Sized:
    """One size of a case: its argv, data bytes (None: measure the output), output path."""

    args: list[str]
    data_bytes: int | None
    output: Path | None


def remove_output(output: Path | None) -> None:
    """Delete an output and anything written beside it (``<output>.history.json``)."""
    if output is None:
        return
    for path in output.parent.glob(f"{output.name}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _fail(message: str) -> None:
    pytest.fail(message, pytrace=False)


def smoke(s: Settings, script: Path, args: list[str], output: Path | None) -> None:
    """Tiny size, no cap, no dask flags: the case's arguments work against today's code."""
    try:
        run = s.run(script, args)
        if not run.ok:
            _fail(f"smoke run failed: {run.describe()}")
    finally:
        remove_output(output)


def _data_bytes(sized: Sized) -> int:
    if sized.data_bytes is not None:
        return sized.data_bytes
    if sized.output is None or not sized.output.exists():
        _fail(f"no output at {sized.output} to measure")
    return zarr_nbytes(sized.output)


def _pair(s: Settings, script, small: Sized, large: Sized, extra: list[str]):
    """Run small then large; return both runs and data sizes. Fails on any error."""
    try:
        run_s = s.run(script, [*small.args, *extra])
        if not run_s.ok:
            _fail(f"small run failed: {run_s.describe()}")
        bytes_s = _data_bytes(small)
    finally:
        remove_output(small.output)
    ceiling = run_s.peak_rss + (large.data_bytes or FETCH_CEILING) + CEILING_SLACK
    try:
        run_l = s.run(script, [*large.args, *extra], limit=ceiling)
        if run_l.killed == "oom":
            return run_s, run_l, bytes_s, large.data_bytes
        if not run_l.ok:
            _fail(f"large run failed: {run_l.describe(ceiling)}")
        bytes_l = _data_bytes(large)
    finally:
        remove_output(large.output)
    return run_s, run_l, bytes_s, bytes_l


def _summary(label, run_s, run_l, bytes_s, bytes_l) -> str:
    growth = run_l.peak_rss - run_s.peak_rss
    delta = (bytes_l or 0) - bytes_s
    killed = f" (killed: {run_l.killed})" if run_l.killed else ""
    return (
        f"{label}: data {fmt_bytes(bytes_s)} -> {fmt_bytes(bytes_l or 0)} (+{fmt_bytes(delta)}), "
        f"peak RSS {fmt_bytes(run_s.peak_rss)} -> {fmt_bytes(run_l.peak_rss)} "
        f"(+{fmt_bytes(growth)}){killed}"
    )


def scaling(s: Settings, script: Path, small: Sized, large: Sized) -> None:
    """Peak memory with the dask flags must grow far less than the data does.

    Runs the same case at two sizes. Passing means the large run's peak RSS
    grew by at most ``max_growth`` x the extra data (inputs for a transform,
    output for a fetcher). A run that grows by the whole data size is killed.

    The small run must already be big enough to fill the dask pipeline (many
    chunks per worker): below that, peak memory ramps up with the data even for
    a skill that streams, and then levels off.
    """
    run_s, run_l, bytes_s, bytes_l = _pair(s, script, small, large, s.dask_args)
    summary = _summary("with dask", run_s, run_l, bytes_s, bytes_l)
    if run_l.killed:
        _fail(f"memory grew with the data.\n{summary}\n{run_l.describe()}")
    delta = bytes_l - bytes_s
    if delta < 4 * bytes_s:
        _fail(f"the large run is not much bigger than the small one; resize the case.\n{summary}")
    allowed = s.max_growth * delta + s.slack
    if run_l.peak_rss - run_s.peak_rss > allowed:
        _fail(
            f"memory grew by more than {s.max_growth:.0%} of the extra data plus "
            f"{fmt_bytes(s.slack)} (allowed +{fmt_bytes(allowed)}).\n{summary}"
        )
    if s.baseline:
        base_s, base_l, base_bytes_s, base_bytes_l = _pair(s, script, small, large, [])
        base = _summary("without dask", base_s, base_l, base_bytes_s, base_bytes_l)
        grew = base_l.peak_rss - base_s.peak_rss
        if not base_l.killed and grew < 0.5 * delta:
            _fail(
                "without dask, memory should grow with the data but did not, so this case "
                f"does not exercise streaming.\n{base}\n{summary}"
            )


def full_size(
    s: Settings, script: Path, args: list[str], output: Path | None, min_output_bytes=None
) -> None:
    """Data several times the cap; with the dask flags the run must stay under it.

    With ``--oom-baseline`` the case first runs without the dask flags and must
    blow the cap, which proves the data really is bigger than memory.
    """
    try:
        if s.baseline:
            base = s.run(script, args, limit=s.limit)
            if base.killed != "oom":
                _fail(
                    "without dask the run should exceed the cap but did not: "
                    f"{base.describe(s.limit)}"
                )
            remove_output(output)
        run = s.run(script, [*args, *s.dask_args], limit=s.limit)
        if not run.ok:
            _fail(f"dask run did not fit: {run.describe(s.limit)}")
        if output is not None and not output.exists():
            _fail(f"no output at {output}\n{run.describe(s.limit)}")
        if output is not None and min_output_bytes is not None:
            size = zarr_nbytes(output)
            if size < min_output_bytes:
                _fail(
                    f"output holds {fmt_bytes(size)}, under {fmt_bytes(min_output_bytes)}: "
                    "the request is not bigger than memory; enlarge it"
                )
    finally:
        remove_output(output)

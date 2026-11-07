"""Run one skill script in its own uv environment under a memory cap.

The skill runs through ``tools/run_with_local_core.sh`` (its PEP 723 deps plus
the sibling weather-skills-core checkout), the same way a user would run it. A
watcher thread samples the resident memory of every Python process under it;
once the sum passes the cap, the whole process group is killed and the run is
reported as ``oom``. Sampling misses spikes shorter than the poll interval, so
the cap is a ceiling on sustained memory, not a hard rlimit.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "tools" / "run_with_local_core.sh"
POLL_SECONDS = 0.05


@dataclass
class Run:
    argv: list[str]
    returncode: int | None
    peak_rss: int
    killed: str | None  # "oom", "timeout", or None
    elapsed: float
    stderr: str

    @property
    def ok(self) -> bool:
        return self.killed is None and self.returncode == 0

    def describe(self, limit: int | None = None) -> str:
        cap = f" (cap {fmt_bytes(limit)})" if limit else ""
        status = self.killed or f"exit {self.returncode}"
        tail = "\n".join(self.stderr.strip().splitlines()[-25:])
        return (
            f"{status} after {self.elapsed:.0f}s, peak RSS {fmt_bytes(self.peak_rss)}{cap}\n"
            f"argv: {' '.join(self.argv)}\n--- stderr (tail) ---\n{tail}"
        )


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n} B"


def parse_bytes(text: str) -> int:
    """``"1GiB"`` / ``"512MiB"`` / ``"2GB"`` / ``"1000000"`` → bytes."""
    s = text.strip().replace(" ", "")
    units = {
        "kib": 2**10,
        "mib": 2**20,
        "gib": 2**30,
        "tib": 2**40,
        "kb": 10**3,
        "mb": 10**6,
        "gb": 10**9,
        "tb": 10**12,
        "b": 1,
    }
    for suffix, scale in units.items():
        if s.lower().endswith(suffix):
            return int(float(s[: -len(suffix)]) * scale)
    return int(float(s))


def python_tree_rss(root: psutil.Process) -> int:
    """Summed RSS of the Python processes at or below ``root`` (skips bash and uv)."""
    total = 0
    try:
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0
    for proc in procs:
        try:
            if "python" in proc.name().lower():
                total += proc.memory_info().rss
        except psutil.Error:
            continue
    return total


def run_skill_capped(
    script: Path,
    args: list[str],
    *,
    limit: int | None,
    timeout: float,
    env: dict[str, str] | None = None,
) -> Run:
    """Run ``script args`` with the local core; kill it past ``limit`` bytes or ``timeout`` s."""
    argv = [str(RUNNER), str(script), *map(str, args)]
    return run_capped(argv, limit=limit, timeout=timeout, env=env)


def run_capped(
    argv: list[str], *, limit: int | None, timeout: float, env: dict[str, str] | None = None
) -> Run:
    """Run ``argv`` in its own process group, watching the RSS of its Python processes."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        start = time.monotonic()
        proc = subprocess.Popen(
            argv,
            stdout=out,
            stderr=err,
            cwd=REPO_ROOT,
            env={**os.environ, **(env or {})},
            start_new_session=True,  # own process group, so one kill reaches uv's child
        )
        watched = psutil.Process(proc.pid)
        peak, killed = 0, None
        while proc.poll() is None:
            rss = python_tree_rss(watched)
            peak = max(peak, rss)
            if limit is not None and rss > limit:
                killed = "oom"
            elif time.monotonic() - start > timeout:
                killed = "timeout"
            if killed:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                break
            time.sleep(POLL_SECONDS)
        elapsed = time.monotonic() - start
        err.seek(0)
        stderr = err.read().decode(errors="replace")
    return Run(argv, proc.returncode, peak, killed, elapsed, stderr)


def zarr_nbytes(path: Path) -> int:
    """Uncompressed size of every data variable in a Zarr, read from metadata only."""
    import xarray as xr

    with xr.open_zarr(path, chunks=None) as ds:
        return int(sum(ds[v].nbytes for v in ds.data_vars))


def stored_chunk_bytes(path: Path) -> int:
    """Uncompressed size of the largest stored chunk of any data variable."""
    import math

    import xarray as xr

    with xr.open_zarr(path, chunks=None) as ds:
        return max(
            (
                math.prod(ds[v].encoding["chunks"]) * ds[v].dtype.itemsize
                for v in ds.data_vars
                if ds[v].encoding.get("chunks")
            ),
            default=0,
        )


@dataclass(frozen=True)
class Input:
    """One synthetic input: ``flag PATH`` with a store of ``kind`` (see oom_synthetic)."""

    flag: str
    kind: str
    variant: tuple = ()  # (key, value) pairs passed to the builder

    @classmethod
    def of(cls, flag: str, kind: str, **variant) -> Input:
        return cls(flag, kind, tuple(sorted(variant.items())))


@dataclass(frozen=True)
class TransformCase:
    """A transform run on synthetic inputs; the input size is set by the tier.

    ``args`` may use ``{tmp}`` for the test's tmp dir. ``output_name`` is the
    output file (export skills write ``.nc``/``.csv``). ``streams=False`` marks
    a skill whose output is built in memory by design (a table), so the memory
    tiers skip it; smoke still checks its arguments.
    """

    id: str
    skill: str
    script: str
    inputs: tuple[Input, ...]
    args: tuple[str, ...] = ()
    output: bool = True  # False for skills with output=False (inspect-zarr)
    output_name: str = "out.zarr"
    streams: bool = True
    # False when the skill streams its input but builds a small output in memory
    # by design (local-extrema's table of extrema): the graph tier then does not
    # require dask arrays at the write.
    lazy_output: bool = True


@dataclass(frozen=True)
class FetchCase:
    """A real fetcher request at two (optionally three) sizes of the same shape.

    ``large_args`` is about 512 MiB - 1 GiB of output, for the memory-scaling tier;
    ``small_args`` is the same request shape at about 1/8 of that, enough to fill the
    dask pipeline. ``full_args``, when set, is several times the full-size cap
    (default 1 GiB) for the full-size data test.
    """

    id: str
    skill: str
    large_args: tuple[str, ...]
    small_args: tuple[str, ...]
    full_args: tuple[str, ...] = ()
    # Credentials, all required. Each is "A|B" alternatives; an alternative is env
    # var names joined by "+", or a file path ("~/.netrc"). Missing ones skip the case.
    creds: tuple[str, ...] = ()
    script: str = "fetch.py"
    note: str = ""  # how the request sizes were worked out

    def missing_creds(self) -> list[str]:
        return [need for need in self.creds if not any(map(_have, need.split("|")))]


def _have(alternative: str) -> bool:
    if "/" in alternative:
        return Path(alternative).expanduser().exists()
    return all(os.environ.get(name) for name in alternative.split("+"))


def skill_script(skill: str, script: str) -> Path:
    return REPO_ROOT / "skills" / skill / "scripts" / script


_DASK_EXTRA = re.compile(r"""["']weather-skills-core\[[^\]]*\bdask\b[^\]]*\]""")


def declares_dask(skill: str, script: str) -> bool:
    """True when the script's PEP 723 header installs ``weather-skills-core[dask]``.

    That extra is how a skill opts into dask mode, so it is also what turns the
    skill's OOM cases from expected failures into required passes.
    """
    return header_declares_dask(skill_script(skill, script).read_text())


def header_declares_dask(text: str) -> bool:
    header = re.search(r"^# /// script$(.*?)^# ///$", text, re.MULTILINE | re.DOTALL)
    return bool(header and _DASK_EXTRA.search(header.group(1)))


def expect_dask(cases, skill_of=lambda c: (c.skill, c.script)):
    """``pytest.param`` per case, strict-xfail for skills that don't declare the dask extra."""
    import pytest

    params = []
    for case in cases:
        skill, script = skill_of(case)
        marks = ()
        if not declares_dask(skill, script):
            marks = pytest.mark.xfail(
                strict=True,
                reason=f"{skill} does not install weather-skills-core[dask] yet",
            )
        params.append(pytest.param(case, id=case.id, marks=marks))
    return params

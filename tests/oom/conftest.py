"""Larger-than-memory (OOM) suite. See tests/oom/README.md.

Tiers, picked per test by a marker:

- ``graph`` (default): in-process, a few MB, counts dask computes before the write.
- ``scaling`` (default): subprocess, small vs large run, memory must not grow with data.
- ``full_size`` (``--oom-full-size``): subprocess, data several times a hard cap.
- ``smoke`` (``--oom-smoke``, runs alone): tiny, no cap, no dask; checks the cases.

Fetcher tests hit the network and also need ``--oom-fetchers``.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest
from oom_harness import parse_bytes
from oom_tiers import Settings

HERE = Path(__file__).parent
TIERS = ("graph", "scaling", "full_size", "smoke")


def pytest_addoption(parser):
    group = parser.getgroup("oom", "larger-than-memory suite (tests/oom)")
    group.addoption(
        "--oom-smoke",
        action="store_true",
        help="Only check the cases: tiny inputs, no cap, no dask flags.",
    )
    group.addoption(
        "--oom-full-size", action="store_true", help="Also run the full-size data test."
    )
    group.addoption(
        "--oom-fetchers", action="store_true", help="Include fetcher cases (real downloads)."
    )
    group.addoption(
        "--oom-baseline",
        action="store_true",
        help="Scaling and full-size also run each case without dask and require it to blow up.",
    )
    group.addoption("--oom-small", default="128MiB", help="Scaling: size of each small input.")
    group.addoption("--oom-large", default="1GiB", help="Scaling: size of each large input.")
    group.addoption(
        "--oom-max-growth",
        type=float,
        default=0.25,
        help="Scaling: allowed peak-memory growth as a fraction of the data growth.",
    )
    group.addoption("--oom-limit", default="1GiB", help="Full size: the memory cap.")
    group.addoption(
        "--oom-factor", type=float, default=3.0, help="Full size: inputs are this many x the cap."
    )
    group.addoption(
        "--oom-dask-args",
        default="--dask --dask-workers 2 --dask-chunk-size 4MiB",
        help="Flags added to every scaling and full-size run.",
    )
    group.addoption("--oom-timeout", type=float, default=3600.0, help="Seconds per skill run.")
    group.addoption(
        "--oom-cache",
        default=None,
        help="Synthetic input cache (default $XDG_CACHE_HOME or ~/.cache, /weather-skills-oom).",
    )


def pytest_configure(config):
    for tier in TIERS:
        config.addinivalue_line("markers", f"{tier}: OOM suite tier (tests/oom)")
    config.addinivalue_line("markers", "fetchers: OOM case that downloads real data")


def _skip_reason(config, item) -> str | None:
    tier = next((t for t in TIERS if item.get_closest_marker(t)), None)
    if tier is None:
        return None
    smoke = config.getoption("--oom-smoke", default=False)
    if smoke != (tier == "smoke"):
        return "--oom-smoke runs only the smoke tier" if smoke else "pass --oom-smoke"
    if tier == "full_size" and not config.getoption("--oom-full-size", default=False):
        return "pass --oom-full-size"
    if item.get_closest_marker("fetchers") and not config.getoption(
        "--oom-fetchers", default=False
    ):
        return "fetcher cases download real data; pass --oom-fetchers"
    return None


def pytest_collection_modifyitems(config, items):
    for item in items:
        if HERE not in Path(item.path).parents:
            continue
        reason = _skip_reason(config, item)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def oom(pytestconfig) -> Settings:
    opt = pytestconfig.getoption
    cache = opt("--oom-cache")
    if cache is None:
        base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
        cache = Path(base) / "weather-skills-oom"
    return Settings(
        small=parse_bytes(opt("--oom-small")),
        large=parse_bytes(opt("--oom-large")),
        max_growth=opt("--oom-max-growth"),
        limit=parse_bytes(opt("--oom-limit")),
        factor=opt("--oom-factor"),
        baseline=opt("--oom-baseline"),
        dask_args=shlex.split(opt("--oom-dask-args")),
        timeout=opt("--oom-timeout"),
        cache=Path(cache),
    )


@pytest.fixture(scope="session")
def dask_in_process():
    """The graph tier needs dask and the local core in pytest's own environment."""
    try:
        import dask  # noqa: F401
    except ImportError:
        pytest.skip(
            "graph tier needs dask in the test env; run it with tools/run_oom_tests.sh "
            "(the shared dev env deliberately has no dask)"
        )

"""Self-tests for the OOM suite's own machinery: the memory watcher and the graph tracker."""

from __future__ import annotations

import sys

import pytest
from oom_harness import FetchCase, header_declares_dask, parse_bytes, run_capped

ALLOC = "import time; b = bytearray({n}); b[::4096] = b'x' * len(b[::4096]); time.sleep({sleep})"


def _alloc(n_bytes: int, sleep: float = 1.0) -> list[str]:
    return [sys.executable, "-c", ALLOC.format(n=n_bytes, sleep=sleep)]


def test_watcher_records_peak_of_a_run_under_the_cap():
    run = run_capped(_alloc(200 * 2**20), limit=1 * 2**30, timeout=30)
    assert run.ok, run.describe()
    assert 200 * 2**20 <= run.peak_rss < 1 * 2**30


def test_watcher_kills_a_run_over_the_cap():
    run = run_capped(_alloc(600 * 2**20, sleep=30), limit=300 * 2**20, timeout=30)
    assert run.killed == "oom", run.describe()
    assert run.elapsed < 10


def test_watcher_kills_a_run_past_the_timeout():
    run = run_capped([sys.executable, "-c", "import time; time.sleep(30)"], limit=None, timeout=1)
    assert run.killed == "timeout"


def test_parse_bytes():
    assert parse_bytes("1GiB") == 2**30
    assert parse_bytes("512MiB") == 512 * 2**20
    assert parse_bytes("2GB") == 2 * 10**9
    assert parse_bytes("1000") == 1000


def test_credential_alternatives(monkeypatch, tmp_path):
    netrc = tmp_path / "netrc"
    case = FetchCase("x", "x", (), (), creds=(f"USER+PASS|{netrc}", "TOKEN"))
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("PASS", "p")
    monkeypatch.delenv("TOKEN", raising=False)
    assert case.missing_creds() == [f"USER+PASS|{netrc}", "TOKEN"]
    netrc.touch()
    monkeypatch.setenv("TOKEN", "t")
    assert case.missing_creds() == []


def test_header_declares_dask():
    def header(*deps):
        lines = "\n".join(f'#   "{d}",' for d in deps)
        return f"# /// script\n# dependencies = [\n{lines}\n# ]\n# ///\nimport os\n"

    assert header_declares_dask(header("weather-skills-core[dask]>=0.4"))
    assert header_declares_dask(header("weather-skills-core[geo,dask]"))
    assert not header_declares_dask(header("weather-skills-core>=0.4", "dask"))
    assert not header_declares_dask("x = 'weather-skills-core[dask]'\n")


@pytest.fixture
def tracker(dask_in_process):
    from oom_graph import track_computes

    return track_computes


def test_tracker_flags_eager_values_before_the_write(tracker, tmp_path):
    import dask.array as da
    import xarray as xr

    ds = xr.Dataset({"v": (("t", "x"), da.ones((64, 1024), chunks=(8, 1024)))})
    with tracker() as report:
        float(ds["v"].sum())  # small result
        _ = ds["v"].values  # whole array: 512 KiB
        ds.to_zarr(tmp_path / "out.zarr")
    pre = [c.result_bytes for c in report.pre_write()]
    assert len(pre) == 2 and max(pre) == 64 * 1024 * 8
    assert [c.phase for c in report.computes][-1] == "write"
    (write,) = report.writes
    assert write.lazy_vars == ["v"] and write.eager_vars == []
    assert write.max_chunk_bytes == 8 * 1024 * 8


def test_tracker_sees_numpy_data_at_the_write(tracker, tmp_path):
    import numpy as np
    import xarray as xr

    ds = xr.Dataset({"v": (("t",), np.ones(10))})
    with tracker() as report:
        ds.to_zarr(tmp_path / "out.zarr")
    assert report.writes[0].eager_vars == ["v"]

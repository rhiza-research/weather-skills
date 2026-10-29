"""Tier 1: run a skill in-process with dask and watch what it computes.

A skill streams when its result stays lazy until the decorator writes it: every
``compute`` before the write should return something small (a coordinate, a
mask, a summary number), and the write should receive dask arrays chunked near
``--dask-chunk-size``. This checks exactly that on a few MB of data, so it runs
in seconds, without measuring memory.

What it cannot see: reads that bypass dask (h5py, rasterio, ``open_dataset``
without ``chunks``). The scaling tier catches those.
"""

from __future__ import annotations

import contextlib
import importlib.util
from dataclasses import dataclass, field

from oom_harness import skill_script


@dataclass
class Compute:
    phase: str  # "skill" (before the write) or "write"
    result_bytes: int


@dataclass
class Write:
    target: str  # "zarr" or "netcdf"
    lazy_vars: list[str]
    eager_vars: list[str]
    max_chunk_bytes: int


@dataclass
class GraphReport:
    phase: str = "skill"
    computes: list[Compute] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)

    def pre_write(self) -> list[Compute]:
        return [c for c in self.computes if c.phase == "skill"]


def _nbytes(obj) -> int:
    if hasattr(obj, "nbytes"):
        return int(obj.nbytes)
    if isinstance(obj, (list, tuple)):
        return sum(_nbytes(o) for o in obj)
    if isinstance(obj, dict):
        return sum(_nbytes(o) for o in obj.values())
    return 8


def _describe_write(ds, target) -> Write:
    lazy, eager, biggest = [], [], 0
    for name, var in ds.data_vars.items():
        if var.chunks is None:
            eager.append(name)
            continue
        lazy.append(name)
        largest = 1
        for sizes in var.chunks:
            largest *= max(sizes) if sizes else 1
        biggest = max(biggest, largest * var.dtype.itemsize)
    return Write(target, lazy, eager, biggest)


@contextlib.contextmanager
def track_computes():
    """Record every dask compute (and which phase it ran in) and each Dataset write."""
    import dask.base
    import xarray as xr

    report = GraphReport()
    orig_get_scheduler = dask.base.get_scheduler

    def get_scheduler(*args, **kwargs):
        schedule = orig_get_scheduler(*args, **kwargs)
        if schedule is None:
            return None

        def counted(dsk, keys, **kw):
            result = schedule(dsk, keys, **kw)
            report.computes.append(Compute(report.phase, _nbytes(result)))
            return result

        return counted

    def wrap_write(method, target):
        def write(self, *args, **kwargs):
            report.writes.append(_describe_write(self, target))
            report.phase = "write"
            try:
                return method(self, *args, **kwargs)
            finally:
                report.phase = "skill"

        return write

    orig_zarr, orig_netcdf = xr.Dataset.to_zarr, xr.Dataset.to_netcdf
    dask.base.get_scheduler = get_scheduler
    xr.Dataset.to_zarr = wrap_write(orig_zarr, "zarr")
    xr.Dataset.to_netcdf = wrap_write(orig_netcdf, "netcdf")
    try:
        yield report
    finally:
        dask.base.get_scheduler = orig_get_scheduler
        xr.Dataset.to_zarr, xr.Dataset.to_netcdf = orig_zarr, orig_netcdf


def load_skill_entry(skill: str, script: str):
    """Import a skill script and return its ``@weather_skill`` entry point."""
    path = skill_script(skill, script)
    name = f"oom_graph_{skill}_{path.stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entries = [
        obj
        for obj in vars(module).values()
        if callable(obj) and hasattr(obj, "parser") and hasattr(obj, "__wrapped__")
    ]
    if len(entries) != 1:
        raise RuntimeError(f"{path}: expected one @weather_skill entry point, found {len(entries)}")
    return entries[0]


def run_in_process(skill: str, script: str, argv: list[str]) -> tuple[int, GraphReport]:
    """Call the skill with ``argv``; return its exit code (0 on return) and the report."""
    entry = load_skill_entry(skill, script)
    with track_computes() as report:
        try:
            entry(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, report

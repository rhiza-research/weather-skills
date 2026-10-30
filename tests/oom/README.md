# Larger-than-memory (OOM) suite

Checks that skills run with `--dask` can process data bigger than memory. The
proof does not need big data: a skill streams when its peak memory stays flat as
the data grows, and when it computes nothing large before the write.

## Running

```bash
tools/run_oom_tests.sh                         # graph + scaling tiers (transforms)
tools/run_oom_tests.sh --oom-fetchers          # ...plus fetchers (real downloads)
tools/run_oom_tests.sh --oom-full-size         # ...plus the full-size data test
tools/run_oom_tests.sh --oom-smoke             # only check the cases themselves
tools/run_oom_tests.sh -k clip-region          # any pytest selection works
```

`tools/run_oom_tests.sh` runs pytest in a throwaway env with dask and the sibling
`weather-skills-core` checkout. The graph tier needs both in pytest's own env, and
the shared dev env deliberately has no dask (installing it would switch every
in-process skill test to dask-backed inputs). Plain `uv run pytest tests/oom` also
works but skips the graph tier. The default `uv run pytest` never collects this
suite (`testpaths` is `skills`).

## Tiers

| Tier | Runs | What passes | Speed |
|---|---|---|---|
| **graph** (default) | In-process, ~8 MiB synthetic input | No compute before the write returns more than a small result; the writer gets dask arrays chunked near `--dask-chunk-size` | Seconds |
| **scaling** (default) | Subprocess: a small input of at least 128 MiB and 8 stored chunks, then 5x that (at least 1 GiB) | Peak RSS grows by at most 25% of the extra data plus 128 MiB (it grows by ~100% if the skill loads everything) | Minutes |
| **full size** (`--oom-full-size`) | Subprocess, ~3 GiB under a 1 GiB cap | The run stays under the cap | Slow |
| **smoke** (`--oom-smoke`, alone) | Subprocess, tiny, no cap, no `--dask` | The case's arguments work against today's code | Minutes |

The graph tier cannot see reads that bypass dask (h5py, rasterio, `open_dataset`
without `chunks`); the scaling tier catches those. `--oom-baseline` makes scaling
and full size also run each case without `--dask` and require it to blow up, which
proves the case actually exercises streaming.

Each case is a **strict expected failure until its skill installs
`weather-skills-core[dask]`** in its script header; from then on it must pass. So
the suite is green while skills migrate, and opting a skill in is what makes its
cases count. `test_coverage.py` fails if a skill has no case.

## Files

- `oom_cases_transforms.py`, `oom_cases_fetchers.py`: the case tables, shared by
  every tier. Fetcher cases carry a `small_args` and a `large_args` request (and
  optionally `full_args`), with the size arithmetic in `note`. The small request is
  the large one at about 1/8 (a smaller window or box, same flags), so it already fills
  the dask pipeline: remote sources are read in whole stored chunks, and a tiny
  request would make that fixed cost look like growth.
- `oom_synthetic.py`: synthetic envelope Zarrs of a target size, written slab by
  slab and cached in `~/.cache/weather-skills-oom` (values rounded, so they compress).
- `oom_harness.py`: runs a skill script through `tools/run_with_local_core.sh` in
  its own process group, samples the summed RSS of its Python processes every
  50 ms, and kills it past a cap or timeout.
- `oom_graph.py`: the in-process compute tracker for the graph tier.
- `oom_tiers.py`: the subprocess tiers' pass/fail logic.
- `test_harness.py`: self-tests for the watcher and the tracker.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--oom-small` / `--oom-large` | `128MiB` / `1GiB` | Scaling input floors; the small run is raised to 8 stored chunks so it fills the dask pipeline, and the large to 5x the small |
| `--oom-max-growth` | `0.25` | Allowed peak growth as a fraction of the data growth |
| `--oom-slack` | `128MiB` | Allowed on top, for run-to-run RSS noise (±100–150 MB) |
| `--oom-limit` / `--oom-factor` | `1GiB` / `3` | Full size: cap, and inputs as a multiple of it |
| `--oom-dask-args` | `--dask --dask-workers 2 --dask-chunk-size 4MiB` | Added to scaling and full-size runs |
| `--oom-timeout` | `3600` | Seconds per skill run |
| `--oom-cache` | `~/.cache/weather-skills-oom` | Synthetic input cache |

Fetcher cases skip when their credentials are missing (an env var, or a file such
as `~/.netrc`). Station/API fetchers come after the gridded ones.

The watcher samples every 50 ms, so it bounds sustained memory, not a spike
shorter than that.

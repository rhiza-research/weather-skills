#!/usr/bin/env bash
# Run the OOM suite (tests/oom) in a throwaway env that has dask and the local core.
#
# The graph tier calls skills in-process, so pytest's own env needs dask and the
# sibling weather-skills-core checkout. The shared dev env deliberately has no dask
# (it would switch every in-process skill test to dask-backed inputs), so this uses
# an isolated env instead. Extra args go to pytest:
#
#   tools/run_oom_tests.sh                      # graph + scaling tiers, transforms
#   tools/run_oom_tests.sh --oom-smoke          # check the cases only
#   tools/run_oom_tests.sh --oom-full-size -k clip-region
#
# Override the core path with WEATHER_SKILLS_CORE=/path/to/weather-skills-core.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORE="${WEATHER_SKILLS_CORE:-$ROOT/../weather-skills-core}"
cd "$ROOT"
# Default to the whole suite unless a path under tests/ is given.
TARGET=(tests/oom)
for arg in "$@"; do
  [[ "$arg" == tests/* ]] && TARGET=()
done
exec uv run --isolated --with "dask[array]" --with-editable "$CORE" pytest ${TARGET[@]+"${TARGET[@]}"} "$@"

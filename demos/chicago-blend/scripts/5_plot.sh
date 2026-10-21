#!/bin/bash
# One panel per lead week; colour range 0-0.6.
set -euo pipefail
cd "$WORK"
for f in p2026_models p2026_models_analog; do
  uv run -q "$PLOT" -i $f.zarr -o "$OUT/$f.png" --spec '{"layout": {"title": {"text": "P(ICPAC onset in lead week | no onset before it), OND 2026"}}, "data": [{"uid": "0", "zmin": 0, "zmax": 0.6, "meta": {"source": {"isel": {"time": 0}}}}]}'
done

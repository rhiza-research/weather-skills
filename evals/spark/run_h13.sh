#!/usr/bin/env bash
# H13 (fit + latency), H4 (CLM-8B held-out), H18 (offline). Run only after bootstrap.sh succeeded.
set -euo pipefail
cd "$(dirname "$0")"
source pins.env
mkdir -p results
./attest.sh > results/attestation.txt

./serve.sh encoder
./serve.sh slm

# Memory sampler for the whole session: GB10 memory is unified, so record both views.
sample_memory() {
  while true; do
    gpu=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
    sys=$(free -m | awk '/Mem:/ {print $3}')
    echo "$(date +%s),${gpu},${sys}"
    sleep 1
  done
}
sample_memory > results/memory.csv &
SAMPLER=$!

# The largest synthetic dataset stays resident while the models serve: memory contention between
# model weights and xarray/Zarr work is the actual H13 question on a 128 GB unified-memory box.
.venv/bin/python hold_dataset.py &
HOLD=$!
trap 'kill "$SAMPLER" "$HOLD" 2>/dev/null || true; ./serve.sh stop' EXIT

for run in 1 2 3; do
  .venv/bin/python bench.py --arms clm,slm --run "$run"
done

echo "== H18: outbound traffic denied; the site must still work"
sudo ufw default deny outgoing
.venv/bin/python bench.py --arms clm,slm --run offline || echo "OFFLINE RUN FAILED"
sudo ufw default allow outgoing

(cd results && sha256sum ./* > MANIFEST.sha256)
echo "RESULTS READY. Pull from your machine with: scp -r <host>:$(pwd)/results ./spark-results"
echo "(nothing is pushed from this box). Then run ./teardown.sh"

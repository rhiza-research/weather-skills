#!/usr/bin/env bash
# Start/stop model servers bound to 127.0.0.1 only (reach them through `ssh -L`, never publicly).
set -euo pipefail
cd "$(dirname "$0")"; source pins.env
case "${1:-}" in
  encoder) docker run -d --rm --name clm-encoder --gpus all -p 127.0.0.1:8090:8090 -v "$PWD/models/encoder:/m:ro" \
             "$VLLM_IMAGE" vllm serve /m --served-model-name qwen3-8b --runner pooling --max-model-len 2048 \
             --port 8090 --gpu-memory-utilization 0.30 >/dev/null
           until curl -sf http://127.0.0.1:8090/v1/models >/dev/null; do sleep 3; done ;;
  slm)     docker run -d --rm --name clm-slm --gpus all -p 127.0.0.1:8091:8091 -v "$PWD/models/slm:/m:ro" \
             "$VLLM_IMAGE" vllm serve /m --served-model-name slm --max-model-len 4096 --port 8091 \
             --gpu-memory-utilization 0.45 >/dev/null
           until curl -sf http://127.0.0.1:8091/v1/models >/dev/null; do sleep 3; done ;;
  stop)    docker rm -f clm-encoder clm-slm >/dev/null 2>&1 || true ;;
  *) echo "usage: serve.sh encoder|slm|stop"; exit 2 ;;
esac

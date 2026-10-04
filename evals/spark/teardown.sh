#!/usr/bin/env bash
# Run AFTER pulling results/. Removes everything this runbook put on the box.
set -uo pipefail
cd "$(dirname "$0")"
source pins.env
./serve.sh stop
docker image rm "${VLLM_IMAGE}" >/dev/null 2>&1 || true
rm -rf models CLM .venv results "${HELDOUT_FILE}" "${HOME}/.cache/huggingface" "${HOME}/.cache/uv"
sudo ufw --force reset >/dev/null
echo "Box cleaned. Still to do, off the box:"
echo "  1. ask the provider to REIMAGE the machine, and keep their confirmation"
echo "  2. delete the rental SSH key pair locally and from the provider account"
echo "  3. close the rental; confirm the time box and budget cap held"

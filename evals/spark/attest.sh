#!/usr/bin/env bash
# Hardware/software attestation, written into the results bundle so a result can be tied to the
# exact box, driver, image and pinned inputs that produced it.
set -uo pipefail
cd "$(dirname "$0")"
source pins.env
echo "date: $(date -u +%FT%TZ)"
uname -a
head -3 /etc/os-release 2>/dev/null
nvidia-smi -q | grep -E "Product Name|Driver Version|CUDA Version|Serial Number" | head -10
echo "vllm image: ${VLLM_IMAGE}"
docker image inspect --format '{{.Id}} {{.RepoDigests}}' "${VLLM_IMAGE}" 2>/dev/null
echo "clm commit: $(git -C CLM rev-parse HEAD 2>/dev/null)"
sha256sum "models/clm/${CLM_HEAD_FILE}" "${HELDOUT_FILE}" 2>/dev/null
.venv/bin/python -c "import sys, torch; print('torch', torch.__version__, 'python', sys.version.split()[0])" 2>/dev/null

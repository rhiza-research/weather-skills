#!/usr/bin/env bash
# One-time setup on the Spark. Fails closed on any unpinned input or failed check.
# Needs outbound HTTPS only to: huggingface.co, github.com, and the vLLM image registry.
set -euo pipefail
cd "$(dirname "$0")"
source pins.env
for v in CLM_COMMIT CLM_HEAD_REV CLM_HEAD_SHA256 ENCODER_REV SLM_REV VLLM_IMAGE HELDOUT_SHA256 OPERATOR_IP; do
  [ -n "${!v:-}" ] || { echo "FATAL: $v is empty in pins.env"; exit 2; }
done
[[ "$VLLM_IMAGE" == *@sha256:* ]] || { echo "FATAL: VLLM_IMAGE must be pinned by digest"; exit 2; }
[ "$(uname -m)" = "aarch64" ] || { echo "FATAL: expected aarch64 (GB10), got $(uname -m)"; exit 2; }
command -v nvidia-smi >/dev/null || { echo "FATAL: nvidia-smi missing"; exit 2; }
command -v uv >/dev/null || { echo "FATAL: install uv first (https://docs.astral.sh/uv/)"; exit 2; }
echo "$HELDOUT_SHA256  $HELDOUT_FILE" | sha256sum -c -

echo "== firewall: inbound SSH from the operator only"
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from "$OPERATOR_IP" to any port 22 proto tcp
sudo ufw --force enable

echo "== vLLM image by digest, then a fail-fast pooling smoke on GB10"
docker pull "$VLLM_IMAGE"

echo "== models, anonymously, at pinned revisions (no HF token on this box)"
export HF_HUB_DISABLE_TELEMETRY=1 HF_TOKEN=
uvx --from 'huggingface_hub[cli]' hf download "$ENCODER_REPO" --revision "$ENCODER_REV" --local-dir models/encoder
uvx --from 'huggingface_hub[cli]' hf download "$SLM_REPO" --revision "$SLM_REV" --local-dir models/slm
uvx --from 'huggingface_hub[cli]' hf download "$CLM_HEAD_REPO" "$CLM_HEAD_FILE" --revision "$CLM_HEAD_REV" --local-dir models/clm_raw
echo "$CLM_HEAD_SHA256  models/clm_raw/$CLM_HEAD_FILE" | sha256sum -c -

echo "== CLM code from source at the pinned commit"
rm -rf CLM && git clone --quiet "$CLM_REPO" CLM && git -C CLM checkout --quiet "$CLM_COMMIT"
[ "$(git -C CLM rev-parse HEAD)" = "$CLM_COMMIT" ] || { echo "FATAL: CLM commit mismatch"; exit 2; }
uv venv -q .venv && uv pip install -q --python .venv -e ./CLM safetensors

echo "== neutralise the pickle: weights_only load, tensors-only check, re-save, delete original"
.venv/bin/python - <<PY
import torch, os
sd = torch.load("models/clm_raw/$CLM_HEAD_FILE", map_location="cpu", weights_only=True)
def walk(x):
    if isinstance(x, torch.Tensor): return
    if isinstance(x, dict): [walk(v) for v in x.values()]; return
    if isinstance(x, (list, tuple)): [walk(v) for v in x]; return
    if isinstance(x, (int, float, str, bool, type(None))): return
    raise SystemExit(f"FATAL: non-tensor object in head: {type(x)}")
walk(sd)
os.makedirs("models/clm", exist_ok=True)
torch.save(sd, "models/clm/$CLM_HEAD_FILE")   # re-written from verified tensors only
print("head verified and re-saved")
PY
rm -rf models/clm_raw
echo "== smoke: vLLM pooling runner on GB10 (fails fast if unsupported)"
./serve.sh encoder && sleep 5 && curl -sf http://127.0.0.1:8090/v1/models >/dev/null && echo "pooling OK"
./serve.sh stop
echo "BOOTSTRAP OK"

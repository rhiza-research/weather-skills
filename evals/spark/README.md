# Running the small-model stack on a DGX Spark (safely)

This measures whether a single DGX Spark (GB10, 128 GB unified memory) can host the whole
small-model stack: a contrastive router (CLM-8B: a Qwen3-8B pooling encoder plus a 75 MB head),
a small generative model (Qwen3.6-35B-A3B), and the datasets the skills work on, at usable
latency, with no network. It answers three questions the hosted experiments cannot:

- **fit and speed:** peak memory with everything resident; p50/p95 decision latency; tokens/s;
- **CLM-8B on held-out tasks:** accuracy on a frozen set of routing states;
- **offline behaviour:** the same run with all outbound traffic denied.

## Where to run it

In order of preference: a Spark we own, or one provided by NVIDIA; then a single-tenant rental
from a provider with a named legal entity and published terms. Do not use a provider that
publishes no legal entity, terms, or data-handling policy.

## Security model

The box is treated as untrusted and holds nothing worth stealing.

- **No secrets on the box.** Models download anonymously at pinned revisions; there is no Hugging
  Face, OpenRouter, GitHub or cloud token. The large reference models run elsewhere.
- **Access.** Use a key pair made for this rental only; connect with `ssh -a` (no agent
  forwarding) and no reverse tunnels. `bootstrap.sh` allows inbound SSH only from `OPERATOR_IP`.
  Model servers bind to `127.0.0.1`; reach them with `ssh -L` if you need to.
- **Supply chain, pinned in `pins.env` and checked by `bootstrap.sh`:**
  - the vLLM image is pinned by digest;
  - the CLM code is cloned at a commit, not installed from PyPI;
  - model revisions are pinned;
  - the CLM head (a PyTorch pickle) is sha256-checked, loaded once with `weights_only=True`,
    verified to contain only tensors, re-saved, and the original deleted.
- **Integrity.** `attest.sh` records the hardware, driver, image, commit and file hashes into the
  results. Results are JSON with a sha256 manifest, pulled with `scp`; nothing is pushed from the
  box. The measurement runs three times.
- **Exit.** `teardown.sh` removes everything, then prints the off-box steps: provider reimage,
  key deletion, closing the rental.

## Steps

1. Fill the three operator values in `pins.env`: `VLLM_IMAGE` (an aarch64/GB10 vLLM image **by
   digest**), `OPERATOR_IP`, and `HELDOUT_SHA256` (provided with `heldout_states.jsonl`).
2. Copy this folder and `heldout_states.jsonl` to the box, then run `./bootstrap.sh`. It stops at
   the first failed check, including a fail-fast test that vLLM's pooling runner works on GB10.
3. `./run_h13.sh`: three measured runs plus an offline run (outbound denied, then restored).
4. From your machine: `scp -r <host>:<path>/results ./spark-results`, then run `./teardown.sh` on
   the box and do the three off-box steps it prints.

A few hours of rental covers it. Set a time box and budget cap with the provider before starting.

## What comes back

`results/`: `attestation.txt`, `memory.csv` (per-second GPU and system memory), `bench_*_<run>.jsonl`
(per-state hits and latency, clustered by goal), `summary_<run>.json`, and `MANIFEST.sha256`.
Accuracy is scored against the same frozen labels as the hosted arms, so the numbers compare
directly.

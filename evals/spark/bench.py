#!/usr/bin/env python3
"""H13 / H4 benchmark on the Spark. Everything is local (127.0.0.1); nothing leaves the box.

  CLM arm : clm.Engine.rank(state, candidates) against the local Qwen3-8B pooling encoder
            (in-process: no clm-serve HTTP port is opened).
  SLM arm : one-token lettered choice from the local vLLM generative server, log-prob scored.
Inputs  : heldout_states.jsonl, one frozen state per line: {id, cluster, state, candidates, optimal},
          with the same candidate text as the foundation-side arms ("skill: description").
Outputs : results/bench_<arm>_<run>.jsonl (per state: top1/top3 hit, latency_ms) and
          results/summary_<run>.json (hit rates, p50/p95 latency, generation tokens/s).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.request
from pathlib import Path

ROUTER_SYSTEM = (
    "You route a weather/climate analysis agent. You see the user's goal, the current "
    "artifacts and history, and the skills that are VALID right now, each with its own "
    "description. Pick the next skill that makes the most progress toward the goal at the "
    "lowest cost. Answer with the single letter of your choice and nothing else."
)
LETTERS = "ABCDEFGHIJKLMNOPQRST"


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def slm_choose(state: str, cands: list[str]) -> tuple[list[float], dict]:
    opts = "\n".join(f"{LETTERS[i]}) {c}" for i, c in enumerate(cands))
    d = post(
        "http://127.0.0.1:8091/v1/chat/completions",
        {
            "model": "slm",
            "temperature": 0,
            "max_tokens": 1,
            "logprobs": True,
            "top_logprobs": 20,
            "messages": [
                {"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": f"STATE: {state}\n\nVALID SKILLS NOW:\n{opts}"},
            ],
        },
    )
    content = (d["choices"][0].get("logprobs") or {}).get("content") or [{}]
    sc = {x["token"].strip().upper(): x["logprob"] for x in content[0].get("top_logprobs", [])}
    return [sc.get(LETTERS[i], -1e9) for i in range(len(cands))], d.get("usage", {})


def tokens_per_second() -> float:
    t = time.perf_counter()
    d = post(
        "http://127.0.0.1:8091/v1/chat/completions",
        {
            "model": "slm",
            "temperature": 0,
            "max_tokens": 256,
            "ignore_eos": True,
            "messages": [{"role": "user", "content": "Describe the water cycle in detail."}],
        },
    )
    return d["usage"]["completion_tokens"] / (time.perf_counter() - t)


def hits(scores: list[float], cands: list[str], optimal: list[str]) -> tuple[int, int]:
    order = sorted(range(len(cands)), key=lambda i: -scores[i])
    names = [cands[i].split(":", 1)[0] for i in order]
    return int(names[0] in optimal), int(bool(set(names[:3]) & set(optimal)))


def p95(xs: list[float]) -> float:
    return sorted(xs)[int(0.95 * (len(xs) - 1))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="clm,slm")
    ap.add_argument("--run", default="1")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    rows = [
        json.loads(x) for x in Path("heldout_states.jsonl").read_text().splitlines() if x.strip()
    ]
    rows = rows[: a.limit] if a.limit else rows
    out = Path("results")
    out.mkdir(exist_ok=True)
    summary: dict = {"run": a.run, "n_states": len(rows)}
    if "clm" in a.arms:
        from clm import Engine

        eng = Engine(emb_url="http://127.0.0.1:8090/v1/embeddings")
        recs, lat = [], []
        for r in rows:
            t = time.perf_counter()
            ranked = eng.rank(r["state"], r["candidates"])
            lat.append((time.perf_counter() - t) * 1000)
            prob = {x["candidate"]: x["prob"] for x in ranked}
            sc = [math.log(max(prob.get(c, 1e-12), 1e-12)) for c in r["candidates"]]
            h1, h3 = hits(sc, r["candidates"], r["optimal"])
            recs.append(
                {
                    "id": r["id"],
                    "cluster": r["cluster"],
                    "top1": h1,
                    "top3": h3,
                    "latency_ms": lat[-1],
                }
            )
        (out / f"bench_clm_{a.run}.jsonl").write_text("\n".join(map(json.dumps, recs)))
        summary["clm"] = {
            "top1": statistics.mean(x["top1"] for x in recs),
            "top3": statistics.mean(x["top3"] for x in recs),
            "p50_ms": statistics.median(lat),
            "p95_ms": p95(lat),
        }
    if "slm" in a.arms:
        recs, lat, toks = [], [], 0
        for r in rows:
            t = time.perf_counter()
            sc, usage = slm_choose(r["state"], r["candidates"])
            lat.append((time.perf_counter() - t) * 1000)
            toks += int(usage.get("prompt_tokens", 0))
            h1, h3 = hits(sc, r["candidates"], r["optimal"])
            recs.append(
                {
                    "id": r["id"],
                    "cluster": r["cluster"],
                    "top1": h1,
                    "top3": h3,
                    "latency_ms": lat[-1],
                }
            )
        (out / f"bench_slm_{a.run}.jsonl").write_text("\n".join(map(json.dumps, recs)))
        summary["slm"] = {
            "top1": statistics.mean(x["top1"] for x in recs),
            "top3": statistics.mean(x["top3"] for x in recs),
            "p50_ms": statistics.median(lat),
            "p95_ms": p95(lat),
            "prompt_tokens_total": toks,
            "gen_tokens_per_s": tokens_per_second(),
        }
    (out / f"summary_{a.run}.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()

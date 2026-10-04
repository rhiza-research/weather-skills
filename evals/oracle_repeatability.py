#!/usr/bin/env python3
"""Oracle repeatability for generated scenarios (spec: >= 3 oracle runs must agree).

    python evals/oracle_repeatability.py --dir evals/generated [--sample 0.1]

Pass 1 runs every scenario's golden via ``run_eval --agent script``; passes 2-3 re-run a
deterministic sample. Reports pass counts per tier and any scenario whose verdict differs between
passes (a flaky oracle is a broken task, not a noisy one). Writes REPEATABILITY.json in --dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_eval import list_scenarios, run_one  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=HERE / "generated")
    ap.add_argument("--sample", type=float, default=0.1)
    a = ap.parse_args()
    dirs = list_scenarios(a.dir)
    sample = [
        d
        for d in dirs
        if int(hashlib.sha256(d.name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < a.sample
    ]
    verdicts: dict[str, list[bool]] = {}
    for pass_no, todo in ((1, dirs), (2, sample), (3, sample)):
        for d in todo:
            verdicts.setdefault(str(d.relative_to(a.dir)), []).append(
                run_one(d, agent="script", keep=None) == 0
            )
        print(f"pass {pass_no}: {len(todo)} scenarios", file=sys.stderr, flush=True)
    tiers: dict[str, dict[str, int]] = {}
    for k, v in verdicts.items():
        t = tiers.setdefault(k.split("/")[0].split("\\")[0], {"n": 0, "pass1": 0})
        t["n"] += 1
        t["pass1"] += v[0]
    flaky = sorted(k for k, v in verdicts.items() if len(set(v)) > 1)
    report = {
        "tiers": tiers,
        "sampled_for_repeats": len(sample),
        "flaky": flaky,
        "failed_pass1": sorted(k for k, v in verdicts.items() if not v[0]),
    }
    (a.dir / "REPEATABILITY.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(
        json.dumps({k: report[k] for k in ("tiers", "sampled_for_repeats")}),
        json.dumps({"flaky": len(flaky), "failed_pass1": len(report["failed_pass1"])}),
    )
    return 0 if not flaky and not report["failed_pass1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

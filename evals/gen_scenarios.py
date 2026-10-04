#!/usr/bin/env python3
"""Deterministic scenario generator for the tiered composition evals.

    python evals/gen_scenarios.py --out evals/generated            # writes <tier>/<family>/<id>/
    python evals/run_eval.py --agent script --scenarios-dir evals/generated/t1   # oracle check

Tiers (EXPERIMENT_SPEC H12): t1 familiar compositions with goal-level prompts; t2 held-out
compositions of familiar pieces; t3 vague natural-language requests; t4 a hidden trap (a
cumulative forecast the prompt does not call cumulative); t5 out-of-catalogue requests whose only
correct answer is an explicit refusal.

Expectations are ORACLE-DERIVED: each scenario's reference chain (golden) is actually run on its
fixture, and time/step sizes, units, aggregation period and mean value are read off the result.
Hand-computing them is error-prone because --end-time is an exclusive edge and convert-to-totals
drops incomplete bins by default. Each scenario folder also gets a golden.py, so
``--agent script`` re-runs the oracle for the repeatability check. Generated folders are not
committed; MANIFEST.json (params + sha256 of each expect.json) is, so a regeneration is verifiable.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import xarray as xr

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tests"))
from conftest import load_skill, run_skill  # noqa: E402
from run_eval import seed_fixture  # noqa: E402

SKILL_FN = {
    "aggregate-temporal": ("aggregate", "aggregate"),
    "convert-to-totals": ("convert_to_totals", "convert_to_totals"),
    "deaccumulate": ("deaccumulate", "deaccumulate"),
}
OUT = "out/result.zarr"


def d(s: str) -> date:
    return date.fromisoformat(s)


def chain_for(family: str, p: dict) -> list[tuple[str, list[str]]]:
    src = p["fixture"]["path"]
    if family in ("weekly_totals", "dekadal_totals"):
        per = "weekly" if family == "weekly_totals" else "dekadal"
        return [
            (
                "aggregate-temporal",
                ["-i", src, "-o", "out/_agg.zarr", "--period", per, "--end-time", p["end"]],
            ),
            ("convert-to-totals", ["-i", "out/_agg.zarr", "-o", OUT]),
        ]
    if family == "weekly_rates":
        return [
            (
                "aggregate-temporal",
                ["-i", src, "-o", OUT, "--period", "weekly", "--end-time", p["end"]],
            )
        ]
    if family in ("deacc_weekly_rates", "hidden_cumulative"):
        return [
            ("deaccumulate", ["-i", src, "-o", "out/_rates.zarr"]),
            ("aggregate-temporal", ["-i", "out/_rates.zarr", "-o", OUT, "--period", "weekly"]),
        ]
    if family == "deacc_weekly_totals":
        return [
            ("deaccumulate", ["-i", src, "-o", "out/_rates.zarr"]),
            (
                "aggregate-temporal",
                ["-i", "out/_rates.zarr", "-o", "out/_agg.zarr", "--period", "weekly"],
            ),
            ("convert-to-totals", ["-i", "out/_agg.zarr", "-o", OUT]),
        ]
    return []


def run_chain(workdir: Path, chain):
    (workdir / "out").mkdir(exist_ok=True)
    for skill, args in chain:
        stem, fn = SKILL_FN[skill]
        mod = load_skill(skill, stem)
        run_skill(getattr(mod, fn), *[str(workdir / a) if a.endswith(".zarr") else a for a in args])


# --- prompts -----------------------------------------------------------------------------


def prompt_for(family: str, style: str, p: dict) -> str:
    f = p["fixture"]
    src, var = f["path"], f["name"]
    last = (d(p["end"]) - timedelta(days=1)).isoformat() if "end" in p else None
    what = {
        "weekly_totals": f"weekly precipitation TOTALS (mm per week) for complete weeks, with the last week ending on {last} (end-time {p.get('end')}, exclusive)",
        "dekadal_totals": f"dekadal (10-day) precipitation TOTALS (mm per dekad) for complete dekads, with the last dekad ending on {last} (end-time {p.get('end')}, exclusive)",
        "weekly_rates": f"weekly MEAN precipitation rates (mm/day), with the last week ending on {last} (end-time {p.get('end')}, exclusive)",
        "deacc_weekly_rates": "weekly MEAN precipitation rates (mm/day) by forecast week",
        "deacc_weekly_totals": "weekly precipitation TOTALS (mm per forecast week)",
        "hidden_cumulative": "weekly MEAN precipitation rates (mm/day) by forecast week",
    }[family]
    desc = {
        "daily_rates": f"`{src}` holds daily precipitation rates (`{var}`, mm/day)",
        "cumulative_forecast": (
            f"`{src}` holds a precipitation forecast (`{var}`) with a forecast step axis"
            if family == "hidden_cumulative"
            else f"`{src}` holds a forecast of precipitation ACCUMULATED since initialization (`{var}`, mm)"
        ),
    }[f["kind"]]
    if style == "vague":
        vague = {
            "weekly_totals": f"How much rain fell each full week up to {last}?",
            "dekadal_totals": f"How much rain fell in each full dekad up to {last}?",
            "weekly_rates": f"What was the average daily rainfall each week up to {last}?",
            "deacc_weekly_rates": "What average daily rain does this forecast give for each week ahead?",
        }[family]
        return (
            f"{desc}.\n\n{vague} Save the result as `{OUT}`.\nUse only the data in the workspace."
        )
    lead = {
        "formal": f"Using the workspace data, {desc}. Produce {what} and write it to `{OUT}`.",
        "terse": f"{desc}. Need: {what}. Output: `{OUT}`.",
        "chatty": f"Hi! {desc}. Could you work out {what}, and save it to `{OUT}`? Thanks.",
    }[style]
    return lead + "\nUse only the data in the workspace; do not fetch anything."


T5_PROMPTS = [
    "Compute the 3-month Standardized Precipitation-Evapotranspiration Index (SPEI) from `{src}`. Save it to `out/result.zarr`.",
    "Bias-correct `{src}` against station observations with quantile mapping and save to `out/result.zarr`.",
    "Train a neural network on `{src}` to predict next week's rainfall and save predictions to `out/result.zarr`.",
    "Estimate reference evapotranspiration (Penman-Monteith) from `{src}` and save it to `out/result.zarr`.",
    "Compute river discharge for the upper Tana basin from `{src}` and save it to `out/result.zarr`.",
    "Derive soil moisture at 10 cm depth from `{src}` and save it to `out/result.zarr`.",
    "Estimate maize yield for this season from `{src}` and save it to `out/result.zarr`.",
    "Compute the El Nino 3.4 index from `{src}` and save it to `out/result.zarr`.",
]


# --- scenario grid -------------------------------------------------------------------------


def grid(seed: int):
    rng = random.Random(seed)
    starts = [(date(2026, 1, 5) + timedelta(days=17 * i)).isoformat() for i in range(24)]
    names = ["precip", "tp", "pr", "rain"]
    spec = []
    styles = ["formal", "terse", "chatty"]
    for fam, tier in (("weekly_totals", "t1"), ("weekly_rates", "t1"), ("dekadal_totals", "t2")):
        for n, st, style in itertools.product((15, 21, 22, 28, 29, 35), starts[:12], styles):
            span = 7 if fam != "dekadal_totals" else 10
            if n < span:
                continue
            off = rng.choice([0, 1, 2])
            end = (d(st) + timedelta(days=n - off)).isoformat()
            fx = {
                "kind": "daily_rates",
                "path": "rates.zarr",
                "n_time": n,
                "start": st,
                "name": rng.choice(names),
                "fill": round(rng.uniform(0.5, 8.0), 2),
            }
            spec.append((tier, fam, style, {"fixture": fx, "end": end}))
    for fam, tier, inits in (
        ("deacc_weekly_rates", "t1", starts[:12]),
        ("deacc_weekly_totals", "t2", starts[:12]),
        ("hidden_cumulative", "t4", starts),
    ):
        for n, init, style in itertools.product(
            (14, 21, 28, 35) if fam != "deacc_weekly_rates" else (14, 21, 28), inits, styles
        ):
            fx = {
                "kind": "cumulative_forecast",
                "path": "forecast.zarr",
                "n_step": n,
                "init": init,
                "name": rng.choice(names),
                "fill": round(rng.uniform(0.5, 8.0), 2),
            }
            spec.append((tier, fam, style, {"fixture": fx}))
    # T3 re-asks familiar compositions vaguely. Draw from every t1 style and de-duplicate on the
    # underlying parameters: the pre-registered floor is >= 250 per tier (formal-only capped it at 180).
    uniq = {
        json.dumps([fam, p], sort_keys=True): (fam, p) for tier, fam, _, p in spec if tier == "t1"
    }
    t3 = [uniq[k] for k in sorted(uniq)]
    spec += [("t3", fam, "vague", p) for fam, p in rng.sample(t3, min(260, len(t3)))]
    for i, (tmpl, st) in enumerate(itertools.product(T5_PROMPTS, starts[:7])):
        fx = {
            "kind": "daily_rates",
            "path": "rates.zarr",
            "n_time": 21,
            "start": st,
            "name": "precip",
            "fill": 2.0,
        }
        spec.append(
            ("t5", "out_of_catalogue", f"t5_{i % len(T5_PROMPTS)}", {"fixture": fx, "t5": tmpl})
        )
    return spec


def build(tier, fam, style, p, out: Path) -> dict:
    sid = hashlib.sha256(json.dumps([tier, fam, style, p], sort_keys=True).encode()).hexdigest()[
        :10
    ]
    folder = out / tier / fam / sid
    folder.mkdir(parents=True, exist_ok=True)
    expect = {
        "mode": "offline",
        "timeout_s": 600,
        "tier": tier,
        "family": fam,
        "style": style,
        "fixtures": [p["fixture"]],
    }
    if tier == "t5":
        prompt = p["t5"].format(src=p["fixture"]["path"])
        expect["expect_cannot"] = True
        (folder / "golden.py").write_text(GOLDEN_CANNOT, encoding="utf-8")
    else:
        prompt = prompt_for(fam, style, p)
        chain = chain_for(fam, p)
        with tempfile.TemporaryDirectory(prefix=f"gen-{sid}-") as tmp:
            w = Path(tmp)
            seed_fixture(w, p["fixture"])
            run_chain(w, chain)
            with xr.open_zarr(w / OUT, consolidated=True) as ds:
                var = p["fixture"]["name"]
                mean = float(ds[var].mean().compute())
                checks = {
                    "has_history": True,
                    "dim_sizes": {k: int(v) for k, v in ds.sizes.items() if k in ("time", "step")},
                    "var_units": {var: [ds[var].attrs.get("units")]},
                    "value_mean": {var: [mean, max(1e-6, 1e-4 * abs(mean))]},
                }
                agg = ds[var].attrs.get("aggregation_period")
                if agg:
                    checks["aggregation_period"] = agg
        expect["skills_used"] = sorted({s for s, _ in chain})
        expect["outputs"] = [{"glob": OUT, "min_count": 1, "checks": checks}]
        (folder / "golden.py").write_text(
            GOLDEN.format(chain=json.dumps(chain), evals=str(HERE)), encoding="utf-8"
        )
    (folder / "prompt.md").write_text(prompt + "\n", encoding="utf-8")
    txt = json.dumps(expect, indent=1, sort_keys=True)
    (folder / "expect.json").write_text(txt + "\n", encoding="utf-8")
    return {
        "id": sid,
        "tier": tier,
        "family": fam,
        "style": style,
        "expect_sha256": hashlib.sha256(txt.encode()).hexdigest(),
    }


GOLDEN = '''#!/usr/bin/env python3
"""Oracle chain for a generated scenario (evals/gen_scenarios.py). No LLM."""
import argparse, sys
from pathlib import Path
sys.path.insert(0, {evals!r})
from gen_scenarios import run_chain
CHAIN = {chain}
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--workdir", type=Path, required=True)
    run_chain(ap.parse_args().workdir, [(s, a) for s, a in CHAIN])
'''


GOLDEN_CANNOT = '''#!/usr/bin/env python3
"""Oracle for an out-of-catalogue scenario: the correct behaviour is an explicit refusal."""
import argparse, json
from pathlib import Path
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--workdir", type=Path, required=True)
    d = ap.parse_args().workdir / "_eval"; d.mkdir(exist_ok=True)
    (d / "usage.json").write_text(json.dumps({"status": "cannot", "by_role": {}, "final": "CANNOT: oracle"}))
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=HERE / "generated")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="smoke: first N per tier")
    a = ap.parse_args()
    spec = grid(a.seed)
    if a.limit:
        kept, seen = [], {}
        for s in spec:
            if seen.get(s[0], 0) < a.limit:
                kept.append(s)
                seen[s[0]] = seen.get(s[0], 0) + 1
        spec = kept
    if a.out.exists():
        shutil.rmtree(a.out)
    rows = []
    for i, s in enumerate(spec):
        rows.append(build(*s, a.out))
        if i % 100 == 0:
            print(f"{i}/{len(spec)}", file=sys.stderr, flush=True)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    manifest = {"seed": a.seed, "counts": counts, "scenarios": rows}
    (a.out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

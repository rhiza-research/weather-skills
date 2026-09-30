"""Two model-driven agents for the composition evals, both over any OpenAI-compatible endpoint
(OpenRouter by default). Standard library only, so normal Weather Skills users install nothing new.

``run_full_agent``  -- the REFERENCE arm: a large model as a full tool-calling agent. It can read
                       any SKILL.md and run any skill, with no eligibility filter and no planner.
``run_stack_agent`` -- the SMALL-MODEL arm: at each step a small model picks ONE lettered option
                       (a valid skill, DONE, or CANNOT) and then fills that skill's CLI args. It
                       escalates a single DECISION (not the conversation) to a reference model when
                       its choice margin is low or a run fails twice.

Both write ``_eval/transcript.jsonl`` and ``_eval/usage.json`` (input, output and reasoning tokens
per role, plus provider-reported cost when present), so remote tokens per workflow (spec H19) come
straight from the run.

Environment: ``EVAL_LLM_API_KEY`` (or ``OPENROUTER_API_KEY``), optional ``EVAL_LLM_BASE_URL``
(default https://openrouter.ai/api/v1). Use synthetic fixtures only: requests go to third-party
providers.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
BASE_URL = os.environ.get("EVAL_LLM_BASE_URL", "https://openrouter.ai/api/v1")
OFFLINE_EXCLUDE_SUFFIX = "-fetch"  # offline scenarios never fetch
CANNOT = "CANNOT"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# --- catalogue ------------------------------------------------------------------


def catalogue(offline: bool = True) -> dict[str, dict]:
    out = {}
    for md in sorted(SKILLS.glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        name = re.search(r"^name:\s*(.+)$", text, re.M)
        desc = re.search(r"^description:\s*(.+)$", text, re.M)
        if not name:
            continue
        n = name.group(1).strip()
        if offline and (n.endswith(OFFLINE_EXCLUDE_SUFFIX) or n == "submit-feedback"):
            continue
        scripts = sorted((md.parent / "scripts").glob("*.py"))
        out[n] = {
            "description": (desc.group(1).strip() if desc else ""),
            "skill_md": md,
            "script": scripts[0] if scripts else None,
        }
    return out


def run_skill(
    cat: dict, name: str, args: list[str], workdir: Path, timeout_s: int = 300
) -> tuple[int, str]:
    if name not in cat or cat[name]["script"] is None:
        return 2, f"unknown or unavailable skill {name!r}"
    proc = subprocess.run(
        [sys.executable, str(cat[name]["script"]), *map(str, args)],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return proc.returncode, (
        proc.stdout[-2000:] + ("\n" + proc.stderr[-2000:] if proc.stderr else "")
    )


def list_files(workdir: Path) -> str:
    rows = []
    for p in sorted(workdir.rglob("*")):
        rel = p.relative_to(workdir)
        if rel.parts[0] == "_eval" or any(x.endswith(".zarr") for x in rel.parts[:-1]):
            continue
        rows.append(str(rel) + ("/" if p.is_dir() else ""))
    return "\n".join(rows) or "(empty)"


# --- transport --------------------------------------------------------------------


class Meter:
    def __init__(self):
        self.by_role: dict[str, dict[str, float]] = {}

    def add(self, role: str, usage: dict | None):
        u = usage or {}
        r = self.by_role.setdefault(
            role, {"calls": 0, "input": 0, "output": 0, "reasoning": 0, "cost": 0.0}
        )
        r["calls"] += 1
        r["input"] += int(u.get("prompt_tokens") or 0)
        r["output"] += int(u.get("completion_tokens") or 0)
        r["reasoning"] += int(
            (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        )
        r["cost"] += float(u.get("cost") or 0.0)


def chat(model: str, messages: list, meter: Meter, role: str, **extra) -> dict:
    key = os.environ.get("EVAL_LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("set EVAL_LLM_API_KEY or OPENROUTER_API_KEY")
    body = {"model": model, "messages": messages, "usage": {"include": True}, **extra}
    for attempt in range(6):
        req = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            json.dumps(body).encode(),
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.load(r)
            meter.add(role, d.get("usage"))
            return d
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"{model}: HTTP {e.code} {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 5:
                raise
            time.sleep(3 * 2**attempt)
    raise RuntimeError("unreachable")


def _log(workdir: Path, rec: dict):
    d = workdir / "_eval"
    d.mkdir(exist_ok=True)
    with (d / "transcript.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _finish(workdir: Path, meter: Meter, status: str, extra: dict | None = None):
    d = workdir / "_eval"
    d.mkdir(exist_ok=True)
    (d / "usage.json").write_text(
        json.dumps({"status": status, "by_role": meter.by_role, **(extra or {})}, indent=1)
    )


SYSTEM_COMMON = (
    "You are a careful weather/climate data analyst working in a workspace of Zarr datasets. "
    "Use only the Weather Skills available to you, and only the data already in the workspace: "
    "never fetch remote data. Prefer absolute YYYY-MM-DD dates. If no sequence of available skills "
    f"can do what is asked, reply with a line starting '{CANNOT}:' and a one-sentence reason, and stop. "
    "Do not improvise computations outside the skills."
)


# --- reference arm: full agent ---------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List workspace files.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Read one skill's SKILL.md.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "Run a skill CLI in the workspace with an argv list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "args"],
            },
        },
    },
]


def run_full_agent(prompt: str, workdir: Path, model: str, max_turns: int = 30) -> tuple[bool, str]:
    cat, meter = catalogue(), Meter()
    listing = "\n".join(f"- {k}: {v['description']}" for k, v in cat.items())
    msgs = [
        {"role": "system", "content": f"{SYSTEM_COMMON}\n\nAVAILABLE SKILLS:\n{listing}"},
        {"role": "user", "content": prompt},
    ]
    for turn in range(max_turns):
        d = chat(model, msgs, meter, "reference", tools=TOOLS)
        m = d["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        _log(
            workdir,
            {"turn": turn, "role": "assistant", "content": m.get("content"), "tool_calls": calls},
        )
        if not calls:
            text = m.get("content") or ""
            _finish(workdir, meter, "cannot" if CANNOT in text else "done", {"final": text[:2000]})
            return True, f"full-agent finished in {turn + 1} turns"
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c["function"]["name"]
            try:
                a = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                a = {}
            if fn == "list_files":
                out = list_files(workdir)
            elif fn == "read_skill":
                p = cat.get(a.get("name", ""), {}).get("skill_md")
                out = p.read_text(encoding="utf-8") if p else f"no such skill {a.get('name')!r}"
            elif fn == "run_skill":
                rc, txt = run_skill(cat, a.get("name", ""), a.get("args") or [], workdir)
                out = f"exit={rc}\n{txt}"
            else:
                out = f"unknown tool {fn!r}"
            _log(
                workdir, {"turn": turn, "role": "tool", "name": fn, "args": a, "result": out[:1500]}
            )
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    _finish(workdir, meter, "max_turns")
    return False, "full-agent hit max_turns"


# --- small-model arm: the stack ----------------------------------------------------------


def eligible_skills(cat: dict, workdir: Path) -> list[str]:
    """Skill-level eligibility. Uses the agentic/ predicates (PR #128) when present, else the
    offline catalogue. Parameter-level validity stays in each skill, as in #128."""
    try:
        import xarray as xr
        from agentic.predicates import eligible_actions  # type: ignore  # PR #128
        from agentic.state import canonical_state  # type: ignore
    except ImportError:
        return list(cat)
    zarrs = [
        p
        for p in sorted(workdir.rglob("*.zarr"))
        if "_eval" not in p.parts
        and not any(x.endswith(".zarr") for x in p.relative_to(workdir).parts[:-1])
    ]
    dss = {
        p.stem: xr.open_zarr(p, consolidated=True) for p in zarrs
    }  # slot = stable stem, never a path
    try:
        ok, _ = eligible_actions(canonical_state({}, dss), list(cat))
    finally:
        for ds in dss.values():
            ds.close()
    return [k for k in cat if k in set(ok)] or list(cat)


def compact_state(workdir: Path, history: list[str]) -> str:
    rows = []
    try:
        import xarray as xr

        for p in sorted(workdir.rglob("*.zarr")):
            if "_eval" in p.parts or any(
                x.endswith(".zarr") for x in p.relative_to(workdir).parts[:-1]
            ):
                continue
            with xr.open_zarr(p, consolidated=True) as ds:
                v = {
                    k: {
                        "units": ds[k].attrs.get("units"),
                        "dims": list(ds[k].dims),
                        "aggregation_period": ds[k].attrs.get("aggregation_period"),
                    }
                    for k in ds.data_vars
                }
                rows.append(f"{p.relative_to(workdir)}: {json.dumps(v)} sizes={dict(ds.sizes)}")
    except Exception as exc:  # noqa: BLE001
        rows.append(f"(state read failed: {exc})")
    return (
        "DATASETS:\n" + ("\n".join(rows) or "none") + f"\nHISTORY: {' > '.join(history) or 'start'}"
    )


def _choose(
    model: str, prompt: str, state: str, opts: list[str], cat: dict, meter: Meter, role: str
):
    lines = [
        f"{LETTERS[i]}) {o}: {cat[o]['description'] if o in cat else ''}"
        for i, o in enumerate(opts)
    ]
    msgs = [
        {
            "role": "system",
            "content": SYSTEM_COMMON + " Answer with the single letter of the next step.",
        },
        {"role": "user", "content": f"TASK:\n{prompt}\n\n{state}\n\nOPTIONS:\n" + "\n".join(lines)},
    ]
    d = chat(model, msgs, meter, role, max_tokens=4, temperature=0, logprobs=True, top_logprobs=10)
    ch = d["choices"][0]
    toks = ((ch.get("logprobs") or {}).get("content")) or []
    letters = LETTERS[: len(opts)]
    scores = {}
    if toks:
        for x in toks[0].get("top_logprobs", []):
            t = x.get("token", "").strip().upper()
            if t in letters and t not in scores:
                scores[t] = x["logprob"]
    if not scores:  # provider returned no logprobs: take the answer, margin unknown (0)
        t = next(
            (c for c in (ch["message"].get("content") or "").strip().upper() if c in letters),
            letters[0],
        )
        return opts[letters.index(t)], 0.0
    p = sorted((math.exp(v) for v in scores.values()), reverse=True) + [0.0]
    best = max(scores, key=scores.get)
    return opts[letters.index(best)], p[0] - p[1]


def _args(
    model: str,
    prompt: str,
    state: str,
    skill: str,
    cat: dict,
    meter: Meter,
    role: str,
    err: str = "",
):
    doc = cat[skill]["skill_md"].read_text(encoding="utf-8")[:6000]
    msgs = [
        {
            "role": "system",
            "content": SYSTEM_COMMON + " Reply with ONLY a JSON array of CLI argument "
            "strings for this one skill invocation; paths relative to the workspace.",
        },
        {
            "role": "user",
            "content": f"TASK:\n{prompt}\n\n{state}\n\nSKILL {skill}:\n{doc}"
            + (f"\n\nPREVIOUS ATTEMPT FAILED:\n{err[-1500:]}" if err else ""),
        },
    ]
    d = chat(model, msgs, meter, role, max_tokens=300, temperature=0)
    text = d["choices"][0]["message"].get("content") or "[]"
    m = re.search(r"\[.*\]", text, re.S)
    try:
        return [str(x) for x in json.loads(m.group(0))] if m else []
    except json.JSONDecodeError:
        return []


def run_stack_agent(
    prompt: str,
    workdir: Path,
    slm: str,
    reference: str | None,
    max_steps: int = 12,
    eps: float = 0.15,
) -> tuple[bool, str]:
    cat, meter, history = catalogue(), Meter(), []
    escalations = 0
    for step in range(max_steps):
        state = compact_state(workdir, history)
        opts = eligible_skills(cat, workdir) + ["DONE", CANNOT]
        pick, margin = _choose(slm, prompt, state, opts, cat, meter, "slm")
        role = "slm"
        if margin < eps and reference:
            pick, _ = _choose(reference, prompt, state, opts, cat, meter, "escalation")
            role, escalations = "escalation", escalations + 1
        _log(workdir, {"step": step, "pick": pick, "margin": margin, "by": role})
        if pick in ("DONE", CANNOT):
            _finish(
                workdir,
                meter,
                "cannot" if pick == CANNOT else "done",
                {"escalations": escalations, "steps": step, "final": pick},
            )
            return True, f"stack {pick.lower()} after {step} steps ({escalations} escalations)"
        err = ""
        for attempt in range(3):
            who = slm if attempt < 2 or not reference else reference
            args = _args(
                who, prompt, state, pick, cat, meter, "slm" if who == slm else "escalation", err
            )
            if who != slm:
                escalations += 1
            rc, txt = run_skill(cat, pick, args, workdir)
            _log(
                workdir, {"step": step, "skill": pick, "args": args, "exit": rc, "out": txt[-800:]}
            )
            if rc == 0:
                history.append(pick)
                break
            err = txt
        else:
            _finish(workdir, meter, "failed", {"escalations": escalations, "steps": step})
            return False, f"stack: {pick} failed 3 times"
    _finish(workdir, meter, "max_steps", {"escalations": escalations})
    return False, "stack hit max_steps"

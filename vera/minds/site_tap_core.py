"""Every short-answer LLM call site in Vera as an application of the candidate matrix (four-minds FM-2 / FM-12; owner,
17 Oct: "there are many applications and models that could be introduced - introduce and test them in parallel").

Wiring each decision point by hand (FM-12 lists research, fabric, commerce, operator...) is a module at a time. But
every generation goes through one path (capability_orchestration.ollama_generate), and a call that answers with a
short label is a decision whoever made it. With `decide.site_capture` on, the path records such calls (the site:
calling module and function, request stage, system-prompt hash; the prompt; the answer) in a capped Redis stream.
Each night discover() finds the sites that behave like decisions - enough calls, a small set of answers that cover
almost all of them - and the matrix's candidates (every small LLM, zero-shot over the site's labels) answer a sample
of the same prompts; agreement with the model that answered live says how replaceable each call site is.

Never captured: chat turns, the shadow judges' own calls (job type idle_decide, stage student), answers longer than
MAX_ANSWER. Agreement only - no gold - so the report is "who could stand in", not "who is right".

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional

STREAM = "vera:decide:sites"
STREAM_MAXLEN = 20000
MAX_ANSWER = 40
MAX_PROMPT = 3000
MAX_SYSTEM = 2000
MIN_CALLS = 30
MAX_LABELS = 12
COVERAGE = 0.9
SKIP_JOB_TYPES = ("idle_decide",)
SKIP_STAGES = ("student", "chat", "chat_stream")
SKIP_MODULE_PREFIXES = ("agents", "chat_", "telegram")
_PUNCT = re.compile(r"^[\s\"'`*_.:;,!?()\[\]{}-]+|[\s\"'`*_.:;,!?()\[\]{}-]+$")


def normalize(answer: Any) -> str:
    """The answer as a label: first line, lower case, without surrounding punctuation or quotes."""
    a = str(answer or "").strip().splitlines()[0] if str(answer or "").strip() else ""
    return _PUNCT.sub("", a).lower()[:MAX_ANSWER]


def wanted(answer: Any, *, job_type: str = "", stage: str = "", module: str = "") -> bool:
    a = str(answer or "").strip()
    if not a or len(a) > MAX_ANSWER or "\n" in a:
        return False
    if (job_type or "") in SKIP_JOB_TYPES or (stage or "") in SKIP_STAGES:
        return False
    return not str(module or "").startswith(SKIP_MODULE_PREFIXES)


def site_key(module: str, func: str, stage: str, system: str) -> str:
    h = hashlib.sha1(str(system or "").encode("utf-8")).hexdigest()[:8]
    return f"{module or '?'}.{func or '?'}|{stage or ''}|{h}"


def entry(caller: Mapping[str, Any], stage: str, system: str, prompt: str, answer: str, model: str) -> Dict[str, str]:
    """One stream entry (all strings, bounded)."""
    module, func = str(caller.get("caller_module") or ""), str(caller.get("caller_func") or "")
    return {"site": site_key(module, func, stage, system), "module": module[:80], "func": func[:80],
            "stage": str(stage or "")[:40], "model": str(model or "")[:120], "system": str(system or "")[:MAX_SYSTEM],
            "prompt": str(prompt or "")[-MAX_PROMPT:], "answer": str(answer or "").strip()[:MAX_ANSWER]}


def discover(entries: Iterable[Mapping[str, Any]], min_calls: int = MIN_CALLS, max_labels: int = MAX_LABELS,
             coverage: float = COVERAGE) -> Dict[str, Dict[str, Any]]:
    """{site: {labels, calls, coverage, module, func, stage}} for the sites that behave like a decision: >= min_calls
    calls whose answers, normalised, are 2..max_labels labels covering >= `coverage` of them."""
    by: Dict[str, List[Mapping[str, Any]]] = {}
    for e in entries:
        if e.get("site"):
            by.setdefault(str(e["site"]), []).append(e)
    out: Dict[str, Dict[str, Any]] = {}
    for site, es in by.items():
        if len(es) < min_calls:
            continue
        c = Counter(normalize(e.get("answer")) for e in es)
        c.pop("", None)
        top = c.most_common(max_labels)
        covered = sum(n for _, n in top) / len(es)
        if len(top) < 2 or covered < coverage:
            continue
        out[site] = {"labels": [lab for lab, _ in top], "calls": len(es), "coverage": round(covered, 3),
                     "module": es[0].get("module"), "func": es[0].get("func"), "stage": es[0].get("stage")}
    return out


def agreement(pairs: Iterable[tuple]) -> Dict[str, Any]:
    """[(live answer, candidate answer, ms)] -> {n, agree, ms_p50}; answers compared normalised."""
    ps = [(normalize(a), normalize(b), m) for a, b, m in pairs]
    if not ps:
        return {"n": 0}
    ms = sorted(int(m or 0) for _a, _b, m in ps)
    return {"n": len(ps), "agree": round(sum(1 for a, b, _m in ps if a and a == b) / len(ps), 3),
            "ms_p50": ms[len(ms) // 2]}


def pick_label(text: str, labels: List[str]) -> Optional[str]:
    """A candidate's free answer as one of the site's labels (exact, else the first label it starts with)."""
    n = normalize(text)
    if n in labels:
        return n
    for lab in labels:
        if n.startswith(lab) or lab.startswith(n) and n:
            return lab
    return n or None

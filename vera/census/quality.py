"""Did the run produce something GOOD, or merely something?

The census has only ever recorded whether a goal finished: done, wall-cap,
error. That is completion, not quality. A run can finish having written a
clock.html with no clock in it, and the census calls that a pass; another can
wall-cap having produced a perfectly good file and be called a failure.
Measured over censuses 30-34, "done" moved between 8/12 and 11/12 while nothing
was recorded about what any of those runs actually built.

This scores a goal against DECLARED, objective expectations - no LLM judgement,
because a model marking its own homework is the false-positive the verifier
gate already exists to prevent, and putting one here would make the measuring
instrument as unreliable as the thing it measures.

A check is a small dict, evaluated against files the run produced and the text
it answered with:

    {"file": "clock.html", "exists": true}
    {"file": "clock.html", "contains": "setInterval"}
    {"file": "clock.html", "regex": "12h|24h|toggle"}
    {"file": "clock.html", "min_bytes": 200}
    {"file": "stats.py",  "any_of": ["def mean", "def median"]}
    {"file": "index.html", "absent": "TODO"}
    {"answer_contains": "391"}

Every check carries a `why` so a failure reads as a finding rather than a
number. Unknown check kinds are reported as errors, never silently passed -
a check that quietly does nothing is worse than no check, because it looks
like coverage.

Pure: no I/O. The caller fetches the files.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _text(files: Dict[str, str], name: str) -> Optional[str]:
    """File content by exact name, else by basename - a run may write
    /workspace/clock.html while the check names clock.html."""
    if name in files:
        return files[name]
    tail = name.rsplit("/", 1)[-1]
    for k, v in files.items():
        if k.rsplit("/", 1)[-1] == tail:
            return v
    return None


def evaluate_one(check: Dict[str, Any], files: Dict[str, str],
                 answer: str = "") -> Dict[str, Any]:
    """One check -> {ok, label, detail}."""
    import re as _re

    why = str(check.get("why") or "").strip()

    if "answer_contains" in check:
        want = str(check["answer_contains"])
        ok = want.lower() in str(answer or "").lower()
        return {"ok": ok, "label": why or ("answer contains %r" % want),
                "detail": "" if ok else "not found in the run's answer"}

    if "answer_regex" in check:
        pat = str(check["answer_regex"])
        try:
            ok = bool(_re.search(pat, str(answer or ""), _re.IGNORECASE | _re.DOTALL))
        except _re.error as e:
            return {"ok": False, "label": why or ("answer matches /%s/" % pat),
                    "detail": "the check's own regex is invalid: %s" % e}
        return {"ok": ok, "label": why or ("answer matches /%s/" % pat),
                "detail": "" if ok else "no match in the run's answer"}

    if "answer_min_words" in check:
        need = int(check["answer_min_words"])
        got = len(str(answer or "").split())
        ok = got >= need
        return {"ok": ok, "label": why or ("answer is at least %d words" % need),
                "detail": "" if ok else "only %d words" % got}

    name = str(check.get("file") or "")
    if not name:
        return {"ok": False, "label": why or "malformed check",
                "detail": "check names neither a file nor answer_contains: %r" % check}

    body = _text(files, name)
    if body is None:
        return {"ok": False, "label": why or ("%s exists" % name),
                "detail": "the run produced no such file"}

    if check.get("exists"):
        return {"ok": True, "label": why or ("%s exists" % name), "detail": ""}

    if "min_bytes" in check:
        need = int(check["min_bytes"])
        ok = len(body) >= need
        return {"ok": ok, "label": why or ("%s is at least %d bytes" % (name, need)),
                "detail": "" if ok else "only %d bytes" % len(body)}

    if "contains" in check:
        want = str(check["contains"])
        ok = want.lower() in body.lower()
        return {"ok": ok, "label": why or ("%s contains %r" % (name, want)),
                "detail": "" if ok else "absent from the file"}

    if "absent" in check:
        bad = str(check["absent"])
        ok = bad.lower() not in body.lower()
        return {"ok": ok, "label": why or ("%s does not contain %r" % (name, bad)),
                "detail": "" if ok else "found in the file"}

    if "any_of" in check:
        wants = [str(w) for w in (check.get("any_of") or [])]
        hit = [w for w in wants if w.lower() in body.lower()]
        ok = bool(hit)
        return {"ok": ok, "label": why or ("%s has any of %s" % (name, wants)),
                "detail": ("matched %r" % hit[0]) if ok else "none of them appear"}

    if "all_of" in check:
        wants = [str(w) for w in (check.get("all_of") or [])]
        missing = [w for w in wants if w.lower() not in body.lower()]
        ok = not missing
        return {"ok": ok, "label": why or ("%s has all of %s" % (name, wants)),
                "detail": "" if ok else "missing %s" % missing}

    if "regex" in check:
        pat = str(check["regex"])
        try:
            ok = bool(_re.search(pat, body, _re.IGNORECASE | _re.DOTALL))
        except _re.error as e:
            return {"ok": False, "label": why or ("%s matches /%s/" % (name, pat)),
                    "detail": "the check's own regex is invalid: %s" % e}
        return {"ok": ok, "label": why or ("%s matches /%s/" % (name, pat)),
                "detail": "" if ok else "no match"}

    return {"ok": False, "label": why or "unknown check",
            "detail": "unsupported check kind: %s" % sorted(check)}


def evaluate(checks: Optional[List[Dict[str, Any]]], files: Dict[str, str],
             answer: str = "") -> Dict[str, Any]:
    """Score a goal. {score, passed, failed, total, results}.

    `score` is None when a goal declares no checks - NOT 0 and NOT 1. An
    unmeasured goal must be visibly unmeasured, or it drags an average toward
    a number nobody computed.
    """
    items = list(checks or [])
    if not items:
        return {"score": None, "passed": 0, "failed": 0, "total": 0, "results": []}
    results = [evaluate_one(c, files or {}, answer) for c in items]
    passed = sum(1 for r in results if r["ok"])
    return {"score": round(passed / float(len(results)), 3),
            "passed": passed, "failed": len(results) - passed,
            "total": len(results), "results": results}


def files_wanted(checks: Optional[List[Dict[str, Any]]]) -> List[str]:
    """Every filename the checks refer to, so the caller knows what to fetch."""
    out: List[str] = []
    for c in (checks or []):
        n = str((c or {}).get("file") or "").strip()
        if n and n not in out:
            out.append(n)
    return out

"""The goal's hard constraints - dates, versions, numeric limits - as checkable items.

Four-minds FM-1.3 / compute-roles B2, as agreed 11 Oct 2026: the planner already reads the goal's
names, so feeding them back adds nothing (census goals carry no dates at all). What CAN act is
the goal's hard constraints: "since March 2024", "Python 3.12", "under 500 words", "top 5". With
switch plan.goal_frame on they become done_when items and the final gate's deterministic
coverage list (FM-5.1's deterministic half), and the run records which of them its answer met.

Pure: text in, constraints / a prompt block / coverage out.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_ISO_DATE = re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b")
_MONTH_YEAR = re.compile(r"\b(%s)\.?\s+((?:19|20)\d{2})\b" % _MONTHS, re.I)
_YEAR = re.compile(r"\b(?:in|since|from|after|before|until|of|during|for)\s+((?:19|20)\d{2})\b", re.I)
_VERSION = re.compile(r"\b([A-Za-z][A-Za-z+#.-]{1,20})\s+v?(\d+\.\d+(?:\.\d+)?)\b")
_WORD_LIMIT = re.compile(r"\b(under|at most|no more than|max(?:imum)?(?: of)?|fewer than|less than|within)\s+"
                         r"(\d{1,5})\s+words?\b", re.I)
_COUNT = re.compile(r"\b(top|at least|exactly|the)\s+(\d{1,3})\s+([a-z]{3,20})\b", re.I)
MAX_ITEMS = 8


def extract(goal: str) -> List[Dict[str, Any]]:
    """[{kind: date|version|word_limit|count, text, token[, n]}], at most MAX_ITEMS, in order."""
    g = str(goal or "")
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(kind: str, text: str, token: str, **extra: Any) -> None:
        k = (kind, token.lower())
        if k in seen or len(out) >= MAX_ITEMS:
            return
        seen.add(k)
        out.append(dict(kind=kind, text=text.strip(), token=token, **extra))

    for m in _ISO_DATE.finditer(g):
        add("date", m.group(0), m.group(0))
    for m in _MONTH_YEAR.finditer(g):
        add("date", m.group(0), m.group(2))
    for m in _YEAR.finditer(g):
        add("date", m.group(0), m.group(1))
    for m in _VERSION.finditer(g):
        add("version", m.group(0), m.group(2))
    for m in _WORD_LIMIT.finditer(g):
        add("word_limit", m.group(0), m.group(2), n=int(m.group(2)))
    for m in _COUNT.finditer(g):
        if m.group(1).lower() == "the" and int(m.group(2)) > 50:
            continue
        add("count", m.group(0), m.group(2), n=int(m.group(2)))
    return out


def block(cons: List[Dict[str, Any]]) -> str:
    """The constraints as a done_when / final-gate block; "" when there are none."""
    if not cons:
        return ""
    return ("GOAL CONSTRAINTS (from the goal text - each must be met, or stated plainly as not met):\n"
            + "\n".join("- %s" % c.get("text") for c in cons))


def coverage(cons: List[Dict[str, Any]], text: str) -> Dict[str, Any]:
    """Which constraints the delivered text meets. date/version/count: the token appears;
    word_limit: the text's word count is within the limit."""
    t = str(text or "")
    tl = t.lower()
    covered, missing = [], []
    for c in cons or []:
        if c.get("kind") == "word_limit":
            ok = len(t.split()) <= int(c.get("n") or 0)
        else:
            ok = str(c.get("token") or "").lower() in tl
        (covered if ok else missing).append(c.get("text"))
    n = len(covered) + len(missing)
    return {"covered": covered, "missing": missing, "ratio": round(len(covered) / n, 3) if n else None}


def writer_block(cons: List[Dict[str, Any]]) -> str:
    """The constraints as instructions for the WRITER (plan.goal_frame=writer, compute-roles R-B2, 16 Oct 2026):
    appended to an authoring call's task, never to done_when or the gate - measured 12 Oct, constraints in
    done_when + the must-cover list sent the loop into self-verification and a research loop that never converged."""
    if not cons:
        return ""
    return ("HARD CONSTRAINTS FROM THE GOAL - honour each one in what you write:\n"
            + "\n".join("- " + str(c.get("text") or "") for c in cons))

"""Info-seeking misses in live chat, as signals (info-seeking plan SK-4.3). Pure: no I/O.

Two kinds, both cheap to see from what the server already has at the end of a turn:
  - a question about the outside world that wants the web (turn_needs says so) answered with no lookup at all - no
    [[cap:]] call to an information capability in the reply (the 8 Oct "latest AI/ML updates" answer was this), or
    answered from the pre-answer snippets alone without info.seek;
  - the user pushing back on the previous answer ("that's out of date", "you made that up").
The turn-end hook counts them per day so the eval's "right tool / no guessing" has a live counterpart.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

LOOKUP_CAPS = ("info.seek", "web.research", "web.search", "web.fetch", "intel.lookup", "reader.page",
               "research.run", "http.get")
_CAP_CALL = re.compile(r"\[\[\s*(?:cap|suggest:cap)\s*:?\s*([a-z][\w.]*)", re.I)
_PUSHBACK = re.compile(
    r"\b((that|this|it)('?s| is| was)\s+(wrong|incorrect|not (right|true|correct)|out of date|outdated|old( news)?|"
    r"made up|not what i asked)|out of date|outdated|old news|you made (that|it) up|that'?s from last (year|month)|"
    r"is that (still )?(true|right|current|correct)\??|are you sure( about that)?\??|that'?s not (true|right)|"
    r"(hallucinat\w*|fabricat\w*)|check (that|it) (again|online)|look it up( properly)?)\b", re.I)


def lookups_called(reply: str) -> list:
    """Information capabilities the reply called through inline [[cap:...]] markers."""
    return [m.group(1).lower() for m in _CAP_CALL.finditer(reply or "") if m.group(1).lower() in LOOKUP_CAPS]


def classify(question: str, reply: str, *, wants_web: bool, prefetched: bool) -> Optional[Dict[str, Any]]:
    """{kind, detail} when a turn that wanted the web did not look anything up, else None.
    kind: "no_lookup" (nothing fetched before or during), "snippets_only" (only the pre-answer snippets)."""
    if not wants_web or not str(reply or "").strip():
        return None
    called = lookups_called(reply)
    if called:
        return None
    if prefetched:
        return {"kind": "snippets_only", "detail": "answered from the pre-answer search snippets without info.seek"}
    return {"kind": "no_lookup", "detail": "a question about the outside world answered with no lookup"}


def pushback(user_text: str) -> Optional[str]:
    """The phrase when the user's message pushes back on the last answer's facts, else None."""
    m = _PUSHBACK.search(str(user_text or "")[:400])
    return m.group(0) if m else None

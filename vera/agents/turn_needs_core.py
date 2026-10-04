"""What a chat turn needs fetched before the model answers - decided by rules, from the words of the turn.

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.agents.turn_needs_core`.

Owner, 2026-10-04: "is it possible for the web context to be triggered deterministically (or using nlp) for user
messages to save the llm having to call a search cap - it can be included in the context to begin with". This is the
rules stage of that decision (context-and-ui CU-T.3); NLP (the Sense frame) and a learned student come later behind the
same function and the same switch.

needs(text) -> {"web": Need, "memory_deep": Need}, each {"on": bool, "confidence": 0..1, "reason": str, "query": str}.
A rule only ever turns a fetch ON; when nothing fires the model can still call a search itself, exactly as before.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, Optional

# asked outright: search for it / look it up
_ASK = re.compile(r"\b(search (the )?(web|internet|online)|search for|look (it |this |that )?up|google|find (it )?online|"
                  r"on the (web|internet)|check online|browse for)\b", re.I)
# things that are only true now
_FRESH = re.compile(r"\b(latest|newest|today'?s?|tonight|right now|currently|current|at the moment|this (morning|week|"
                    r"month|year)|breaking|news|headlines?|price|prices|stock|shares?|exchange rate|weather|forecast|"
                    r"what'?s the score|final score|score of|who won|results? of the|release notes|released|"
                    r"just (announced|released|launched)|is .{1,40} down)\b", re.I)   # an outage counts with "today/now"
_MARKET = re.compile(r"\b(btc|eth|bitcoin|ethereum|nasdaq|s&p|ftse|dow jones|usd|gbp|eur)\b", re.I)
# Vera's own world - a freshness word about these is a memory question, not a web one
_INTERNAL = re.compile(r"\b(vera|census|loop ?lab|pipeline|sandbox|worktree|board item|bleeding.edge|the canvas|lhm|"
                       r"my (agent|notes?|diary|calendar|session|portfolio|account|positions?)|this chat|"
                       r"capabilit(y|ies)|the gate|gpu gate|agentic loop|the dashboard|the estate)\b", re.I)
# referring back
_PAST = re.compile(r"\b(remember|last time|earlier|before|previously|we (discussed|talked|decided|said)|you (said|told|"
                   r"suggested)|i (said|told you|asked)|as before|again|what did (i|we|you)|the plan (for|we)|"
                   r"(the|that|my) (research |last )?(decision|report|notes|plan|summary|review) (on|about|for))\b", re.I)
_URL = re.compile(r"https?://\S+", re.I)
_YEAR = re.compile(r"\b(20\d\d)\b")


def _need(on: bool, confidence: float, reason: str, query: str = "") -> Dict[str, Any]:
    return {"on": bool(on), "confidence": round(float(confidence), 2), "reason": reason, "query": query}


def search_query(text: str) -> str:
    """The words worth searching for: the message without the asking."""
    q = _ASK.sub(" ", str(text or ""))
    q = re.sub(r"\b(please|can you|could you|would you|for me|tell me|i want to know)\b", " ", q, flags=re.I)
    q = re.sub(r"^\s*(for|about|on|up)\b", " ", " ".join(q.split()), flags=re.I)   # "search the web FOR x" -> "x"
    return " ".join(q.split())[:200]


def needs(text: Any, *, today: Optional[date] = None, knowledge_year: int = 0) -> Dict[str, Dict[str, Any]]:
    t = " ".join(str(text or "").split())
    out = {"web": _need(False, 0.0, "nothing asks for the web"),
           "memory_deep": _need(False, 0.0, "nothing refers back")}
    if not t:
        return out
    internal = bool(_INTERNAL.search(t))
    past = bool(_PAST.search(t))
    if past or (internal and _FRESH.search(t)):
        out["memory_deep"] = _need(True, 0.8 if past else 0.6,
                                   "refers back" if past else "asks what is latest in Vera's own work")
    if _URL.search(t):
        return out   # a URL is fetched as the page itself, not searched for
    if _ASK.search(t):
        out["web"] = _need(True, 0.95, "asked to search", search_query(t))
        return out
    yr = [int(y) for y in _YEAR.findall(t)]
    ky = int(knowledge_year or 0)
    if ky and any(y > ky for y in yr) and not internal:
        out["web"] = _need(True, 0.8, "a year past the model's knowledge", search_query(t))
        return out
    fresh, market = _FRESH.search(t), _MARKET.search(t)
    if (fresh or market) and not internal:
        why = ("a market figure" if market and not fresh else "something only true now: “" + fresh.group(0) + "”")
        out["web"] = _need(True, 0.75 if fresh else 0.7, why, search_query(t))
    return out

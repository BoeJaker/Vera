"""How relevant a capability is to a query - the scoring behind
CapabilityIndex.relevance_search (caps.search, and the agentic loop's planner
catalogue via _workshop_build_toolkit).

Measured 2026-09-27 on prod: capability search ran with 0 of 2,554 caps
embedded, so it was purely lexical, and the lexical part matched every query
word as a SUBSTRING of every name part at +3 each. The planner catalogue for
"Create clock.html ..." carried markets.custom.create, agent.create,
dream.think.create and census.live; the one for "explain a race condition"
carried ide.vscode.password.reveal and netscan.target.traceroute ("race" in
"traceroute"). Of 29-38 caps offered per run, 1-8 were used.

The rules here:
  * query words that are function words, or GENERIC words that half the
    registry is named with (create, list, write, report, html...), never score
    by name - the meaning (the embedding) decides those;
  * a name or tag scores on WHOLE-WORD matches only ("race" is not "trace");
  * the embedding similarity is the main signal when vectors exist.

Pure: the index hands in the entry and the query's vector.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set

STOP: Set[str] = {
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "via", "is",
    "are", "was", "be", "have", "has", "can", "will", "that", "this", "from", "by", "it", "its",
    "then", "into", "use", "using", "any", "all", "each", "under", "sure", "what", "which", "who",
    "how", "your", "you", "our", "only", "also", "not", "but", "out", "one", "two", "there",
    "their", "them", "they", "when", "where", "while", "about", "should", "must", "would", "could",
    "does", "did", "done", "been", "being", "more", "most", "some", "such", "than", "too", "very",
}
#: Words so many capability names carry that a name match means nothing.
GENERIC: Set[str] = {
    "create", "write", "read", "list", "get", "set", "run", "show", "make", "add", "update",
    "delete", "new", "file", "files", "data", "page", "report", "build", "save", "change",
    "check", "give", "find", "short", "small", "simple", "value", "values", "live", "current",
    "state", "summary", "result", "results", "output", "text", "html", "app", "tool", "tools",
    "info", "information", "status", "item", "items", "thing", "things",
}

W_NAME = 2.0        # a whole-word match in the capability's name
W_TAG = 1.0         # ... in one of its tags
W_KEYWORD = 0.5     # ... in its description keywords
W_CATEGORY = 1.0    # the query names the capability's category
W_EMBED = 8.0       # cosine similarity of query and capability (0..1)

_TOKEN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9]{2,}\b")


def query_tokens(query: str) -> Set[str]:
    """The query words that may score lexically."""
    return {w.lower() for w in _TOKEN.findall(query or "")} - STOP - GENERIC


def name_parts(name: str) -> Set[str]:
    return set(str(name or "").lower().replace(".", " ").replace("_", " ").replace("-", " ").split())


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


#: The planner catalogue's DISCOVERED tail. Measured over 34 census runs: the
#: loop only ever called caps from a small core (exec.*, code.author/edit,
#: prose.author, sandbox fs read, web.*, http.get, operator.run, memory.seek)
#: plus what the goal named; the 15-20 discovered extras added noise, not
#: tools. The tail is now short and must be earned.
MAX_TAIL = 8
ALWAYS_TOP = 3


def names_domain(name: str, query: str, tags: Iterable[str] = ()) -> bool:
    """The query names this cap's domain: one of its non-generic words is a
    whole word of the cap's name or tags ("browser", "markets", "math")."""
    toks = query_tokens(query)
    return bool(toks & (name_parts(name) | {str(t).lower() for t in tags or ()}))


def select_tail(ranked_names: Sequence[str], query: str, *, tags_of=None,
                max_tail: int = MAX_TAIL, always_top: int = ALWAYS_TOP,
                exclude: Iterable[str] = ()) -> List[str]:
    """The discovered caps a planner is offered, from a relevance ranking: the
    best `always_top`, then only caps whose domain the query names, at most
    `max_tail` in all."""
    skip = set(exclude or ())
    out: List[str] = []
    for i, n in enumerate(n for n in ranked_names if n not in skip):
        if len(out) >= max_tail:
            break
        tags = tags_of(n) if tags_of else ()
        if i < always_top or names_domain(n, query, tags):
            out.append(n)
    return out


#: How much of a description a planner's catalogue line carries. It used to be
#: a hard 120-character cut, mid-sentence - which dropped the "WHEN to use"
#: guidance many descriptions put after their first sentence.
BRIEF_MAX = 240
_WHEN = re.compile(r"(?:\bWHEN\b[^.]*|\b[Uu]se (?:it |this )?(?:when|for|instead)\b[^.]*|\bPREFER\b[^.]*)\.?")


def brief_line(name: str, description: str, max_chars: int = BRIEF_MAX) -> str:
    """'name — description' for a planner catalogue: whole sentences up to
    `max_chars`, and the description's own when-to-use sentence even when it
    comes later. Never cut mid-word."""
    d = " ".join(str(description or "").split())
    if not d:
        return name
    def _head(text: str, room: int) -> str:
        if len(text) <= room:
            return text
        cut = text[:max(0, room - 1)]
        sp = cut.rfind(" ")
        return (cut[:sp] if sp > room * 0.5 else cut).rstrip(",;:—- ") + "…"

    # The when-to-use clause is what a planner most needs from a description:
    # its room is reserved first, and the sentences fill what is left.
    m = _WHEN.search(d)
    when = m.group(0).strip() if m else ""
    reserve = min(len(when) + 3, max_chars // 2) if when else 0
    budget = max_chars - reserve
    sents = re.split(r"(?<=[.!?])\s+", d)
    out = ""
    nxt = ""
    for s in sents:
        if len(out) + len(s) + 1 > budget:
            nxt = s
            break
        out = (out + " " + s).strip()
    if not out:                                        # one long first sentence
        out = _head(d, budget)
    elif nxt and len(out) < budget * 0.6 and not (when and when.startswith(nxt[:20])):
        out = out + " " + _head(nxt, budget - len(out) - 1)   # a short first sentence
    if when and when not in out:
        room = max_chars - len(out) - 3                # " | "
        if room > 20:
            out += " | " + _head(when, room)
    return "%s — %s" % (name, out)


def score(name: str, *, tags: Iterable[str] = (), keywords: Iterable[str] = (),
          category: str = "", query: str = "", q_tokens: Optional[Set[str]] = None,
          q_emb: Optional[Sequence[float]] = None,
          embedding: Optional[Sequence[float]] = None) -> float:
    toks = query_tokens(query) if q_tokens is None else q_tokens
    parts = name_parts(name)
    tagset = {str(t).lower() for t in tags or ()}
    kwset = {str(k).lower() for k in keywords or ()}
    s = 0.0
    for t in toks:
        if t in parts:
            s += W_NAME
        if t in tagset:
            s += W_TAG
        if t in kwset:
            s += W_KEYWORD
    cat = str(category or "").replace("_", " ").strip().lower()
    if cat and cat != "general" and cat in (query or "").lower():
        s += W_CATEGORY
    if q_emb and embedding:
        s += cosine(q_emb, embedding) * W_EMBED
    return s

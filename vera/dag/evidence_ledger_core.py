"""The run's dated evidence: every info.seek item a loop collected, kept for the checks.

Probes 4-8 Oct 2026 (research-report): the report files were right and the final answers
invented dates and licences, because nothing checked the answer's claims against the dated
sources the run had read - step history keeps only a clipped preview. info.seek returns a pack
of numbered, dated items ({n, title, url, published, text, ...}); this keeps them per run, so
the answer check can (a) show the checker the evidence and (b) run grounding_rules.check
(answer, items) - the deterministic floor: bad [n] citations, figures no source contains.

Pure (an in-process, bounded per-run store, like research_sources_core).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List

MAX_RUNS = 64
MAX_ITEMS = 40
TEXT_CHARS = 1200
BLOCK_CHARS = 9000
_RUN: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()


def items_from(result: Any) -> List[Dict[str, Any]]:
    """The items of an info.seek pack (the cap's result or its {result: ...} wrapper)."""
    r = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
    if not isinstance(r, dict):
        return []
    out = []
    for it in r.get("items") or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or it.get("snippet") or it.get("claim_or_snippet") or "")
        out.append({"n": it.get("n"), "title": str(it.get("title") or "")[:200],
                    "url": str(it.get("url") or "")[:300], "published": str(it.get("published") or "")[:40],
                    "kind": str(it.get("kind") or ""), "text": text[:TEXT_CHARS]})
    return out


def add(session_id: str, result: Any) -> int:
    """Keep a pack's items for the run (new urls only). Returns how many were added."""
    if not session_id:
        return 0
    new = items_from(result)
    if not new:
        return 0
    have = _RUN.setdefault(session_id, [])
    _RUN.move_to_end(session_id)
    seen = {(i.get("url"), i.get("title")) for i in have}
    added = 0
    for it in new:
        k = (it.get("url"), it.get("title"))
        if k in seen:
            continue
        seen.add(k)
        have.append(it)
        added += 1
    del have[:-MAX_ITEMS]
    while len(_RUN) > MAX_RUNS:
        _RUN.popitem(last=False)
    return added


def items(session_id: str) -> List[Dict[str, Any]]:
    return list(_RUN.get(session_id) or [])


def block(evidence: List[Dict[str, Any]], max_chars: int = BLOCK_CHARS) -> str:
    """The evidence as one prompt block: '[n] (published) title - url' then the text."""
    if not evidence:
        return ""
    share = max(300, max_chars // max(1, len(evidence)))
    parts = []
    for it in evidence:
        head = "[%s]%s %s%s" % (it.get("n", "?"), (" (%s)" % it["published"]) if it.get("published") else "",
                                it.get("title") or "", (" - " + it["url"]) if it.get("url") else "")
        parts.append(head + "\n" + str(it.get("text") or "")[:share])
    return "\n\n".join(parts)[:max_chars]


def forget(session_id: str) -> None:
    _RUN.pop(session_id, None)

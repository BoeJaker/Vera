"""The run's dated evidence: every info.seek item a loop collected, kept for the checks.

Probes 4-8 Oct 2026 (research-report): the report files were right and the final answers
invented dates and licences, because nothing checked the answer's claims against the dated
sources the run had read - step history keeps only a clipped preview. info.seek returns a pack
of numbered, dated items ({n, title, url, published, text, ...}); this keeps them per run, so
the answer check can (a) show the checker the evidence and (b) run grounding_rules.check
(answer, items) - the deterministic floor: bad [n] citations, figures no source contains.

Numbering (review vera-03, 10 Oct): every pack numbers its items from [1]. Each item keeps its
pack-local `pack_n` and gets a run-wide `e` (E1, E2, ...). With ONE pack the answer's [n] are
exactly the pack's - citations can be checked. With several packs an [n] is ambiguous, so
citation checking is off (check_items() returns n=None) and only the figures/dates floor runs.
Every item is kept for the check (MAX_ITEMS bounds memory, not the check); only block() is cut.
Each pack's need / since / gaps are kept too - "no source dated on or after X" is the best
"say so" signal the checker can get.

Pure (an in-process, bounded per-run store, like research_sources_core).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List

MAX_RUNS = 64
MAX_ITEMS = 400
TEXT_CHARS = 1200
BLOCK_CHARS = 9000
_RUN: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _pack(result: Any) -> Dict[str, Any]:
    r = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
    return r if isinstance(r, dict) else {}


#: Tools whose results are evidence: info.seek packs (items, dated), web.research (sources,
#: read pages, undated) and web.fetch (one page). 11 Oct 2026: the I2 probes never called
#: info.seek - the planner kept web.research - so the ledger stayed empty and the grounding
#: floor never ran; web.* evidence still lets it catch figures no source contains.
EVIDENCE_TOOLS = ("info.seek", "web.research", "web.fetch")


def _raw_items(pk: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(pk.get("items"), list):
        return [i for i in pk["items"] if isinstance(i, dict)]
    if isinstance(pk.get("sources"), list):                 # web.research
        return [dict(s, n=s.get("n") or k + 1) for k, s in enumerate(pk["sources"]) if isinstance(s, dict)]
    if pk.get("url") and (pk.get("text") or pk.get("content")):   # web.fetch: one page
        return [{"n": 1, "url": pk.get("url"), "title": pk.get("title") or pk.get("url"),
                 "text": pk.get("text") or pk.get("content"), "published": pk.get("published") or ""}]
    return []


def items_from(result: Any) -> List[Dict[str, Any]]:
    """The items of an evidence pack: info.seek items, web.research sources or a web.fetch page
    (the cap's result or its {result: ...} wrapper)."""
    out = []
    for it in _raw_items(_pack(result)):
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or it.get("snippet") or it.get("claim_or_snippet") or "")
        out.append({"pack_n": it.get("n"), "title": str(it.get("title") or "")[:200],
                    "url": str(it.get("url") or "")[:300], "published": str(it.get("published") or "")[:40],
                    "kind": str(it.get("kind") or ""), "text": text[:TEXT_CHARS]})
    return out


def add(session_id: str, result: Any, step: Any = None) -> int:
    """Keep a pack's items (new urls only) and its need/since/gaps. Returns items added."""
    if not session_id:
        return 0
    pk = _pack(result)
    new = items_from(result)
    if not new and not pk.get("gaps"):
        return 0
    run = _RUN.setdefault(session_id, {"items": [], "packs": []})
    _RUN.move_to_end(session_id)
    pack_no = len(run["packs"]) + 1
    run["packs"].append({"pack": pack_no, "step": step, "need": str(pk.get("need") or ""),
                         "since": str(pk.get("since") or ""),
                         "answerable": pk.get("answerable"),
                         "guide": str(pk.get("guide") or "")[:300],
                         "gaps": [str(g)[:240] for g in (pk.get("gaps") or [])][:6]})
    have = run["items"]
    seen = {(i.get("url"), i.get("title")) for i in have}
    added = 0
    for it in new:
        k = (it.get("url"), it.get("title"))
        if k in seen:
            continue
        seen.add(k)
        it = dict(it, e=len(have) + 1, pack=pack_no)
        have.append(it)
        added += 1
    del have[MAX_ITEMS:]
    while len(_RUN) > MAX_RUNS:
        _RUN.popitem(last=False)
    return added


def items(session_id: str) -> List[Dict[str, Any]]:
    return list((_RUN.get(session_id) or {}).get("items") or [])


def packs(session_id: str) -> List[Dict[str, Any]]:
    return list((_RUN.get(session_id) or {}).get("packs") or [])


def check_items(session_id: str) -> List[Dict[str, Any]]:
    """Items for grounding_rules.check: with one pack n = the pack's own [n] (citations are
    exact); with several, n=None (an [n] is ambiguous - only figures/dates are checked)."""
    its = items(session_id)
    single = len({i.get("pack") for i in its}) <= 1
    return [dict(i, n=(i.get("pack_n") if single else None)) for i in its]


def block(evidence: List[Dict[str, Any]], max_chars: int = BLOCK_CHARS,
          packs_info: List[Dict[str, Any]] = ()) -> str:
    """The evidence as one prompt block: 'E<e> (pack p [n]) (published) title - url' then the
    text, then each pack's need/since/gaps."""
    if not evidence and not packs_info:
        return ""
    share = max(300, max_chars // max(1, len(evidence) or 1))
    parts = []
    for it in evidence:
        head = "E%s (pack %s [%s])%s %s%s" % (
            it.get("e", "?"), it.get("pack", "?"), it.get("pack_n", "?"),
            (" (%s)" % it["published"]) if it.get("published") else "", it.get("title") or "",
            (" - " + it["url"]) if it.get("url") else "")
        parts.append(head + "\n" + str(it.get("text") or "")[:share])
    for p in packs_info or []:
        if p.get("gaps") or p.get("answerable") is False:
            parts.append("pack %s (need=%s, since %s)%s gaps: %s" % (
                p.get("pack"), p.get("need") or "?", p.get("since") or "-",
                " NOT answerable;" if p.get("answerable") is False else "",
                " | ".join(p.get("gaps") or []) or "-"))
    return "\n\n".join(parts)[:max_chars]


WRITER_RULE = ("Cite these sources as [E<n>] - the E numbers below - with their dates. Use only "
               "what they say; where they do not cover something, say so instead of filling it in.")


def writer_sources(session_id: str, max_chars: int = BLOCK_CHARS) -> str:
    """The run's evidence for a writer (prose.author): the citing rule, the info.seek guide if
    any, then the E-numbered block. "" when the run has none. The writer cites [E<n>] - ids
    that are unique across packs, so the answer check can check every citation exactly."""
    its = items(session_id)
    if not its:
        return ""
    guide = next((p.get("guide") for p in packs(session_id) if p.get("guide")), "")
    head = WRITER_RULE + (("\n" + guide) if guide else "")
    return head + "\n\n" + block(its, max_chars=max_chars, packs_info=packs(session_id))


def forget(session_id: str) -> None:
    _RUN.pop(session_id, None)

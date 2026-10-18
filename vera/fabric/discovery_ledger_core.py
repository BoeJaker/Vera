"""discovery.page_on_topic as a ledger decision (four-minds FM-7.5; first pipelines item 9(d)).

A crawl's topic-drift guard decides, for every page past the seeds, whether the page is about the topic: kept
(enough of the topic's distinctive terms) or skipped (too little coverage, or a learned sibling term), with an
LLM second opinion on borderline pages when `llm_drift_gate` is on (off by default). Each verdict becomes a
Decision v0 row (`on_topic` / `off_topic`, decider rule or llm), and what the guard saw goes to the training
store: the topic, the page's title, URL and a short excerpt, and the scores. Those rows are what the 200-page
labelled discovery set is labelled from (/decide/labels), the gate for a System 1 student between the heuristic
and the LLM (ROADMAP FM-7.5: card similarity between the page frame and the topic brief).

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

try:
    from Vera.vera.minds import ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.minds import ledger_core as LC  # type: ignore

QUESTION = "discovery.page_on_topic"
LABELS = ["on_topic", "off_topic"]
EXCERPT_CHARS = 700                     # what the guard's LLM second opinion reads
RULE = "rule:drift_guard"


def page_ref(url: str) -> str:
    return hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:16]


def _r(x: Any) -> Optional[float]:
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return None


def page_decision(url: str, keep: bool, *, crawl_id: str = "", llm: bool = False, model: str = "",
                  relevance: Any = None, coverage: Any = None, neg_hit: bool = False, depth: int = 0,
                  origin: str = "discovery") -> Dict[str, Any]:
    """The guard's verdict on one page. llm=True when the LLM second opinion decided it."""
    detail = {"relevance": _r(relevance), "coverage": _r(coverage), "neg_hit": bool(neg_hit), "depth": int(depth or 0)}
    return LC.make_decision(
        QUESTION, "on_topic" if keep else "off_topic", origin=origin, surface="discovery", qtype="binary",
        labels=list(LABELS), state=dict(detail), state_ref={"kind": "crawl_page", "id": page_ref(url)},
        decider="llm" if llm else "rule", implementation=(f"llm:{model}" if model else "llm:drift") if llm else RULE,
        mode="llm_only" if llm else "rule", run_id=str(crawl_id or "")[:80] or None, detail=detail)


def page_inputs(topic: str, title: str, text: str, url: str, *, relevance: Any = None, coverage: Any = None,
                neg_hit: bool = False, depth: int = 0) -> Dict[str, Any]:
    """What the guard saw, for the training store (which redacts and bounds it)."""
    return {"topic": str(topic or ""), "title": str(title or "")[:300], "url": str(url or "")[:500],
            "excerpt": str(text or "")[:EXCERPT_CHARS], "relevance": _r(relevance), "coverage": _r(coverage),
            "neg_hit": bool(neg_hit), "depth": int(depth or 0)}


# ── discovery.collect_worth (four-minds FM-7.5 scout/collect; web-ingress WI-8b) ──
COLLECT_QUESTION = "discovery.collect_worth"
COLLECT_LABELS = ["collect", "skip"]
COLLECT_RULE = "rule:frontier"


def collect_decision(url: str, collect: bool, *, crawl_id: str = "", score: Any = None, parts: Any = None,
                     rank: int = 0, depth: int = 0, origin: str = "discovery") -> Dict[str, Any]:
    """The frontier's choice for one link: what its score was made of (topic, authority, novelty...), its rank on
    the page, and whether it was queued - the Decision v0 a System 1 student can learn the frontier from."""
    detail = {"score": _r(score), "rank": int(rank or 0), "depth": int(depth or 0),
              "parts": {str(k): _r(v) for k, v in dict(parts or {}).items()}}
    return LC.make_decision(
        COLLECT_QUESTION, "collect" if collect else "skip", origin=origin, surface="discovery", qtype="binary",
        labels=list(COLLECT_LABELS), state=dict(detail), state_ref={"kind": "crawl_link", "id": page_ref(url)},
        decider="rule", implementation=COLLECT_RULE, mode="rule", run_id=str(crawl_id or "")[:80] or None,
        detail=detail)

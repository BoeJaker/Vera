"""The targeted index's query (shared-planning web-ingress WI-14; PLAN §13.3). Pure.

fabric.map.search answers "what has Vera already read on this?" from the page index's full-text hits, then ranks them
with what the web map knows about each URL: whether it belongs to the topic asked about, how recently it was checked,
how often it changes, and how new it was when first seen. The result says, per page, since when Vera has known it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

TOPIC_BOOST = 0.3
FRESH_DAYS = 30
FRESH_BOOST = 0.1
NOVEL_BOOST = 0.1
EXCERPT = 400


def _ts(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def rank(pages: Iterable[Mapping[str, Any]], url_rows: Mapping[str, Mapping[str, Any]],
         topic_members: Iterable[str] = (), *, keys: Optional[Mapping[str, str]] = None,
         now: Optional[datetime] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Index hits (url, title, full_text, rank) ranked with the map: `url_rows` by url_key, `topic_members` url_keys,
    `keys` url -> url_key. The full-text rank is normalised to 0..1 within the result and stays the main term."""
    now = now or datetime.now(timezone.utc)
    ps = list(pages or [])
    if not ps:
        return []
    top = max(float(p.get("rank") or 0.0) for p in ps) or 1.0
    members = set(topic_members or ())
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, p in enumerate(ps):
        url = str(p.get("url") or "")
        k = (keys or {}).get(url, url)
        if not url or k in seen:
            continue
        seen.add(k)
        row = url_rows.get(k) or {}
        base = float(p.get("rank") or 0.0) / top if top else 1.0 / (i + 1)
        last = _ts(row.get("last_seen"))
        first = _ts(row.get("first_seen"))
        fresh = last is not None and (now - last).days <= FRESH_DAYS
        nov = row.get("novelty")
        score = base + (TOPIC_BOOST if k in members else 0.0) + (FRESH_BOOST if fresh else 0.0) + \
            (NOVEL_BOOST * float(nov) if nov is not None else 0.0)
        text = " ".join(str(p.get("full_text") or "").split())
        out.append({"url": url, "key": k, "title": str(p.get("title") or "")[:200], "excerpt": text[:EXCERPT],
                    "score": round(score, 4), "text_rank": round(base, 4), "topic_member": k in members,
                    "known_since": first.isoformat() if first else "", "last_checked": last.isoformat() if last else "",
                    "fetches": int(row.get("fetches") or 0), "changes": int(row.get("changes") or 0),
                    "novelty": nov, "kind": row.get("kind") or "page", "dataset_id": p.get("dataset_id") or ""})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:max(1, int(limit or 10))]


def gap_rows(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """web_queries 'gap' rows grouped: a question the index could not answer, how often, when last, and whether a
    gap crawl has filled it since it was last asked (open = never filled, or asked again after the fill)."""
    out = []
    for r in rows or ():
        last = _ts(r.get("last"))
        filled = _ts(r.get("filled"))
        out.append({"query": str(r.get("query") or ""), "times": int(r.get("n") or 0),
                    "last": last.isoformat() if last else "", "callers": list(r.get("callers") or []),
                    "filled": filled.isoformat() if filled else "",
                    "open": filled is None or (last is not None and filled < last)})
    return out


FILL_MIN_WORDS = 2


def fill_plan(gaps: Iterable[Mapping[str, Any]], *, max_gaps: int = 10) -> List[str]:
    """The gaps a fill run crawls, most asked first: open ones only, one per normalised question, none of one word
    (a lone word is a lookup, not a subject to crawl)."""
    out: List[str] = []
    seen = set()
    for g in gaps or ():
        q = " ".join(str(g.get("query") or "").split())
        k = q.lower()
        if not g.get("open", True) or not q or k in seen or len(q.split()) < FILL_MIN_WORDS:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= max(1, int(max_gaps or 10)):
            break
    return out

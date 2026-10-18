"""The targeted index's query (shared-planning web-ingress WI-14; PLAN §13.3). Pure.

fabric.map.search answers "what has Vera already read on this?" from the page index's full-text hits, then ranks them
with what the web map knows about each URL: whether it belongs to the topic asked about, how recently it was checked,
how often it changes, and how new it was when first seen. The result says, per page, since when Vera has known it.
"""
from __future__ import annotations

import re
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


# ── how we know it works (PLAN §13.3, WI-14c) ─────────────────────────────────

OVERLAP_K = 10


def overlap_at(index_keys: Iterable[str], live_keys: Iterable[str], k: int = OVERLAP_K) -> Optional[float]:
    """Share of the live engine's top k that the index's top k also holds; None when the live list is empty."""
    live = [x for x in live_keys or () if x][:k]
    if not live:
        return None
    idx = set([x for x in index_keys or () if x][:k])
    return round(sum(1 for x in live if x in idx) / len(live), 3)


def daily_metrics(purpose_counts: Mapping[str, int], overlaps: Iterable[Optional[float]],
                  fresh: Iterable[Mapping[str, Any]], crawl: Mapping[str, Any], fetches: int, *,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """The five §13.3 measures from the day's rows. index answer rate = questions the index answered / all asked
    (index_hit + gap); live overlap = mean of the sampled overlaps; freshness lag = days since the newest page per
    topic (median and worst); novel yield = new pages per crawl hour; cost = pages fetched per question asked."""
    now = now or datetime.now(timezone.utc)
    hit, gap = int(purpose_counts.get("index_hit") or 0), int(purpose_counts.get("gap") or 0)
    asked = hit + gap
    ov = [float(x) for x in overlaps or () if x is not None]
    lags = []
    for r in fresh or ():
        t = _ts(r.get("newest"))
        if t is not None:
            lags.append((str(r.get("topic_key") or ""), round((now - t).total_seconds() / 86400, 2)))
    lags.sort(key=lambda x: x[1])
    hours = int(crawl.get("hours") or 0)
    new = int(crawl.get("new") or 0)
    return {
        "questions": asked, "index_hits": hit, "gaps": gap,
        "index_answer_rate": round(hit / asked, 3) if asked else None,
        "live_overlap": round(sum(ov) / len(ov), 3) if ov else None, "overlap_samples": len(ov),
        "freshness_lag_days": {"median": lags[len(lags) // 2][1] if lags else None,
                               "worst": lags[-1] if lags else None, "topics": len(lags)},
        "novel_yield_per_crawl_hour": round(new / hours, 1) if hours else None,
        "crawl": {"fetched": int(crawl.get("fetched") or 0), "new": new, "hours": hours},
        "fetches": int(fetches or 0),
        "pages_per_question": round(int(fetches or 0) / asked, 1) if asked else None,
    }


# ── the hybrid query (WI-14d, PLAN §13.3 "the query") ─────────────────────────

RRF_K = 60
RERANK_WEIGHT = 0.5


def fuse(legs: Mapping[str, Iterable[Mapping[str, Any]]], key_of, *, k: int = RRF_K,
         weights: Optional[Mapping[str, float]] = None) -> List[Dict[str, Any]]:
    """Reciprocal rank fusion of ranked page lists ({leg: [page, ...]} in rank order), one row per url_key: the
    page fields from the leg with the longest text, `rank` = sum of weight / (k + position), `legs` = which legs
    found it. Scale-free, so a cosine and a ts_rank never meet as numbers."""
    rows: Dict[str, Dict[str, Any]] = {}
    for leg, pages in legs.items():
        w = float((weights or {}).get(leg, 1.0))
        for pos, p in enumerate(pages or ()):
            url = str(p.get("url") or "")
            key = key_of(url) if url else ""
            if not key:
                continue
            row = rows.get(key)
            if row is None:
                row = rows[key] = dict(p, rank=0.0, legs=[])
            elif len(str(p.get("full_text") or "")) > len(str(row.get("full_text") or "")):
                row.update({kk: v for kk, v in p.items() if kk not in ("rank", "legs")})
            if leg not in row["legs"]:
                row["legs"].append(leg)
                row["rank"] += w / (k + pos + 1)
    return sorted(rows.values(), key=lambda r: r["rank"], reverse=True)


def squash(x: Any) -> float:
    """A reranker's raw score (a logit) to 0..1."""
    import math
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 <= v <= 1.0:
        return v
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, v))))


def apply_rerank(pages: List[Dict[str, Any]], ranked: Iterable[Mapping[str, Any]],
                 weight: float = RERANK_WEIGHT) -> List[Dict[str, Any]]:
    """Blend a reranker's verdicts ({index, score} over `pages`' order) into the fused rank: rank = (1-w) * fused
    (normalised to the top) + w * reranker. Pages the reranker did not score keep their fused share only."""
    if not pages:
        return pages
    top = max(float(p.get("rank") or 0.0) for p in pages) or 1.0
    rr: Dict[int, float] = {}
    for x in ranked or ():
        try:
            rr[int(x.get("index"))] = squash(x.get("score"))
        except (TypeError, ValueError):
            continue
    if not rr:
        return pages
    out = []
    for i, p in enumerate(pages):
        q = dict(p)
        q["rerank"] = round(rr[i], 4) if i in rr else None
        q["rank"] = (1 - weight) * float(p.get("rank") or 0.0) / top + (weight * rr[i] if i in rr else 0.0)
        out.append(q)
    return sorted(out, key=lambda r: r["rank"], reverse=True)


def record_page(rid: str, dataset_id: str, text: str, data: Any, score: float) -> Optional[Dict[str, Any]]:
    """A fabric record as a page for the vector leg: only records that carry an http(s) url."""
    import json as _json
    try:
        d = _json.loads(data) if isinstance(data, str) and data else (data if isinstance(data, dict) else {})
    except ValueError:
        d = {}
    url = str(d.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return None
    return {"url": url, "title": str(d.get("title") or "")[:200], "full_text": str(text or d.get("text") or ""),
            "dataset_id": dataset_id or "", "rank": float(score or 0.0), "record_id": rid}


# ── is longer retention worth it (PLAN §14.2) ────────────────────────────────

AGE_BUCKETS = ("<1d", "1-7d", "7-30d", "30-60d", "60-90d", ">90d")
AGE_SQL = ("CASE WHEN {d} < interval '1 day' THEN '<1d' WHEN {d} < interval '7 days' THEN '1-7d' "
           "WHEN {d} < interval '30 days' THEN '7-30d' WHEN {d} < interval '60 days' THEN '30-60d' "
           "WHEN {d} < interval '90 days' THEN '60-90d' ELSE '>90d' END")


def retention_curves(revisits: Iterable[Mapping[str, Any]], recall: Iterable[Mapping[str, Any]],
                     sizes: Mapping[str, int], counts: Mapping[str, int], span_days: float) -> Dict[str, Any]:
    """The §14.2 curves from bucketed rows. revisits: {bucket ('first' or an age bucket), n, unchanged,
    comparable}; recall: {bucket, n} for the pages index answers cited, by their age; sizes: table -> bytes;
    counts: table -> rows; span_days: how many days of receipts there are.
    - revisit rate per bucket = fetches whose previous fetch of the URL was that old / all fetches;
    - unchanged rate per bucket = same content hash (or 304) / revisits with a hash on both sides;
    - recall by age = share of cited pages first seen that long before the answer;
    - cost per day kept = bytes of the map tables / days of receipts, and bytes per receipt / link."""
    rv = {str(r.get("bucket")): r for r in revisits or ()}
    total = sum(int(r.get("n") or 0) for r in rv.values())
    curve = {}
    for b in AGE_BUCKETS:
        r = rv.get(b) or {}
        n, comp = int(r.get("n") or 0), int(r.get("comparable") or 0)
        curve[b] = {"revisits": n, "revisit_rate": round(n / total, 4) if total else None,
                    "unchanged_rate": round(int(r.get("unchanged") or 0) / comp, 4) if comp else None}
    rc_rows = {str(r.get("bucket")): int(r.get("n") or 0) for r in recall or ()}
    cited = sum(rc_rows.values())
    total_bytes = sum(int(v or 0) for v in (sizes or {}).values())
    per = lambda t: round(int((sizes or {}).get(t) or 0) / int((counts or {}).get(t) or 1)) if (counts or {}).get(t) else None
    return {"fetches": total, "first_fetches": int((rv.get("first") or {}).get("n") or 0), "by_age": curve,
            "recall_by_age": {b: (round(rc_rows.get(b, 0) / cited, 4) if cited else None) for b in AGE_BUCKETS},
            "cited": cited, "span_days": round(float(span_days or 0), 2),
            "cost": {"map_bytes": total_bytes,
                     "bytes_per_day": round(total_bytes / span_days) if span_days and span_days > 0 else None,
                     "bytes_per_receipt": per("web_fetches"), "bytes_per_link": per("web_links")}}


# ── worldview concept seeds (WI-15, PLAN §13.4; vera-62's terms, 18 Oct) ─────

SEED_MIN_POP, SEED_MAX_POP, SEED_CAP = 3, 19, 10
GENERIC_LABEL_WORDS = frozenset({"fragments", "fragment", "misc", "miscellaneous", "various", "diverse", "samples",
                                 "sample", "mixed", "assorted", "general", "clustering", "other", "unsorted"})


def _title(text: Any) -> str:
    return " ".join(str(text or "").strip().split("\n", 1)[0].split())[:80]


def concept_seeds(concepts: Iterable[Mapping[str, Any]], last_seeded: Mapping[int, Any],
                  cap: int = SEED_CAP) -> List[Dict[str, Any]]:
    """The concepts tonight's crawl seeds (vera-62): population 3-19 only (singletons and doubletons are mostly dead
    codebook codes), never a generic label, at most `cap`, never-seeded first then the longest ago; the query is the
    label plus the titles of two member records, so the search hits the concept and not only its label's words."""
    rows = []
    for c in concepts or ():
        pop = int(c.get("population") or 0)
        label = " ".join(str(c.get("label") or "").split())
        if not (SEED_MIN_POP <= pop <= SEED_MAX_POP) or not label:
            continue
        if set(re.findall(r"[a-z]+", label.lower())) & GENERIC_LABEL_WORDS:
            continue
        idx = int(c.get("idx"))
        titles = [t for t in (_title(m.get("text")) for m in (c.get("members_sample") or [])[:2]) if t]
        rows.append({"idx": idx, "label": label, "population": pop,
                     "query": " ".join([label] + titles)[:300], "last": _ts(last_seeded.get(idx))})
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r["last"] is not None, r["last"] or epoch, r["population"]))
    for r in rows:
        r.pop("last")
    return rows[:max(0, int(cap if cap is not None else SEED_CAP))]


def seed_effect(seeded: Iterable[Mapping[str, Any]], current: Mapping[int, int]) -> List[Dict[str, Any]]:
    """Per seeded concept: population at its first seeding, now, and the change (worldview's own count judges it)."""
    first: Dict[int, Dict[str, Any]] = {}
    for r in seeded or ():
        i = int(r.get("concept_idx"))
        if i not in first:
            first[i] = dict(r)
            first[i]["times"] = 0
            first[i]["pages"] = 0
        first[i]["times"] += 1
        first[i]["pages"] += int(r.get("pages") or 0)
    out = []
    for i, r in first.items():
        before, now = int(r.get("population_at") or 0), current.get(i)
        out.append({"idx": i, "label": r.get("label") or "", "population_at_first_seed": before,
                    "population_now": now, "change": (now - before) if now is not None else None,
                    "seedings": r["times"], "pages": r["pages"]})
    return sorted(out, key=lambda x: x["idx"])

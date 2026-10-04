"""Intelligence - prediction markets as indicators (pure: parsing and series only).

A prediction market turns an open question into a number: "will the BoE cut by
December?" is 31% today and 54% next week. Two public, keyless sources:

- Polymarket: the Gamma API's public search (events with their markets, each with
  outcome prices and one CLOB token per outcome) and the CLOB `prices-history`
  endpoint for a token's price over time;
- Manifold: market search and the bet log, whose `probAfter` is the market's
  probability after each bet.

Nothing here does I/O; the capabilities fetch and hand the JSON in.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MANIFOLD = "https://api.manifold.markets/v0"
SOURCES = ("polymarket", "manifold")


def _jl(v: Any) -> List[Any]:
    """Gamma returns lists as JSON strings ('["Yes", "No"]')."""
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_ms(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def polymarket_market(m: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """One Gamma market as a candidate: the YES side (or the first outcome), its
    token for the price history, its current probability. None when closed or
    unusable."""
    if not isinstance(m, dict) or m.get("closed") or m.get("active") is False:
        return None
    outs, prices, toks = _jl(m.get("outcomes")), _jl(m.get("outcomePrices")), _jl(m.get("clobTokenIds"))
    if not toks:
        return None
    i = next((k for k, o in enumerate(outs) if str(o).strip().lower() == "yes"), 0)
    if i >= len(toks):
        return None
    ev = event or {}
    q = str(m.get("question") or ev.get("title") or "").strip()
    if not q:
        return None
    slug = ev.get("slug") or m.get("slug") or ""
    return {"source": "polymarket", "id": str(m.get("id") or ""), "token": str(toks[i]), "question": q,
            "outcome": str(outs[i]) if i < len(outs) else "Yes",
            "event": str(ev.get("title") or ""), "url": f"https://polymarket.com/event/{slug}" if slug else "",
            "prob": _f(prices[i]) if i < len(prices) else None,
            "volume": _f(m.get("volumeNum") if m.get("volumeNum") is not None else m.get("volume")) or 0.0,
            "end": str(m.get("endDate") or ev.get("endDate") or "")}


def polymarket_search(payload: Dict[str, Any], limit: int = 20) -> List[Dict[str, Any]]:
    """Candidates from a Gamma `public-search` answer, open markets only."""
    out: List[Dict[str, Any]] = []
    for ev in (payload or {}).get("events") or []:
        if not isinstance(ev, dict) or ev.get("closed"):
            continue
        for m in ev.get("markets") or []:
            c = polymarket_market(m, ev)
            if c:
                out.append(c)
    out.sort(key=lambda c: -(c.get("volume") or 0))
    return out[:limit]


def manifold_market(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A Manifold market as a candidate: binary and unresolved only."""
    if not isinstance(m, dict) or m.get("isResolved") or str(m.get("outcomeType") or "") != "BINARY":
        return None
    q = str(m.get("question") or "").strip()
    if not q or not m.get("id"):
        return None
    return {"source": "manifold", "id": str(m["id"]), "token": "", "question": q, "outcome": "Yes", "event": "",
            "url": str(m.get("url") or ""), "prob": _f(m.get("probability")),
            "volume": _f(m.get("volume")) or 0.0, "end": _iso_ms(m.get("closeTime")),
            "bettors": int(m.get("uniqueBettorCount") or 0)}


def manifold_search(payload: Any, limit: int = 20) -> List[Dict[str, Any]]:
    out = [c for c in (manifold_market(m) for m in (payload or []) if isinstance(payload, list)) if c]
    return out[:limit]


def polymarket_series(payload: Dict[str, Any]) -> List[Tuple[float, float]]:
    """(epoch seconds, probability) from a CLOB `prices-history` answer, oldest first."""
    pts = []
    for h in (payload or {}).get("history") or []:
        t, p = _f((h or {}).get("t")), _f((h or {}).get("p"))
        if t is not None and p is not None:
            pts.append((t, p))
    return sorted(pts)


def manifold_series(bets: Any) -> List[Tuple[float, float]]:
    """(epoch seconds, probability after the bet) from a Manifold bet log (newest
    first as served), oldest first; redemptions do not move the price."""
    pts = []
    for b in bets or []:
        if not isinstance(b, dict) or b.get("isRedemption"):
            continue
        t, p = _f(b.get("createdTime")), _f(b.get("probAfter"))
        if t is not None and p is not None:
            pts.append((t / 1000.0, p))
    return sorted(pts)


def resample(points: Sequence[Tuple[float, float]], step: float = 3600.0, until: Optional[float] = None,
             max_points: int = 400) -> Tuple[List[float], List[float]]:
    """A regular series (last value in each step, carried forward through quiet
    steps) so the change detectors see bars, not irregular trades."""
    if not points:
        return [], []
    pts = sorted(points)
    start = (pts[0][0] // step) * step
    end = until if until is not None else pts[-1][0]
    end = (end // step) * step
    n = int((end - start) // step) + 1
    if n > max_points:
        start = end - (max_points - 1) * step
        n = max_points
    ts, vals = [], []
    j, last = 0, None
    while j < len(pts) and pts[j][0] < start:
        last = pts[j][1]
        j += 1
    for k in range(n):
        edge = start + (k + 1) * step
        while j < len(pts) and pts[j][0] < edge:
            last = pts[j][1]
            j += 1
        if last is not None:
            ts.append(start + k * step)
            vals.append(last)
    return ts, vals


def ref(c: Dict[str, Any]) -> str:
    """A stable indicator dataset id for a market."""
    return f"pm:{c.get('source')}:{c.get('token') or c.get('id')}"


def label(c: Dict[str, Any], n: int = 60) -> str:
    q = str(c.get("question") or "")
    return (q if len(q) <= n else q[: n - 1].rstrip() + "…")

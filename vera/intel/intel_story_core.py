"""Stories: the same news carried by several sources, as one object (pure).

Triage already joins an item that is the same story as a signal it holds
(embedding cosine >= 0.92) instead of making a second signal: the newcomer's url
goes on the signal's `also` list. That makes a story implicit - a signal plus its
`also` urls - with no record of when each source joined. This module makes it
explicit:

* `add_join(signal, url, at)` records the join (url, site, time) next to `also`,
  so how fast a story spreads can be read back;
* `story(signal, now)` and `stories(signals, now)` give the view the panel and
  reports read: first seen, how many sources and distinct sites carry it, when the
  last one joined, and momentum (sources that joined in the last 48 hours).

Signals that predate join times still count their sources (from `also`); only
their momentum is unknown and reads 0.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

MAX_JOINS = 24
MOMENTUM_HOURS = 48


def site(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def add_join(signal: Dict[str, Any], url: str, at: str) -> Dict[str, Any]:
    """The fields to update when `url` joins `signal`'s story at `at` (ISO): `also` and `joins`."""
    also = list(signal.get("also") or [])
    joins = list(signal.get("joins") or [])
    if url and url not in also:
        also = (also + [url])[-12:]
    if url and not any(j.get("url") == url for j in joins):
        joins = (joins + [{"url": url, "site": site(url), "at": at}])[-MAX_JOINS:]
    return {"also": also, "joins": joins}


def story(signal: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    urls = [signal.get("url") or ""] + list(signal.get("also") or [])
    sites = sorted({site(u) for u in urls if u} - {""})
    joins = [j for j in signal.get("joins") or [] if isinstance(j, dict)]
    times = sorted(t for t in (_ts(j.get("at")) for j in joins) if t)
    recent = [t for t in times if now - t < timedelta(hours=MOMENTUM_HOURS)]
    return {"id": signal.get("id"), "topic_id": signal.get("topic_id"), "title": signal.get("title", ""),
            "url": signal.get("url", ""), "kind": signal.get("kind", ""), "score": signal.get("score"),
            "first_seen": signal.get("seen", ""), "sources": len([u for u in urls if u]) or 1,
            "sites": sites, "last_join": times[-1].strftime("%Y-%m-%dT%H:%M:%SZ") if times else "",
            "momentum": len(recent), "summary": signal.get("summary") or signal.get("snippet") or ""}


def stories(signals: Sequence[Dict[str, Any]], now: Optional[datetime] = None, *, min_sources: int = 2,
            limit: int = 30) -> List[Dict[str, Any]]:
    """Stories carried by at least `min_sources` sources, strongest first: momentum,
    then distinct sites, then sources, then score, then the newest first seen."""
    now = now or datetime.now(timezone.utc)
    rows = [story(s, now) for s in signals]
    rows = [r for r in rows if r["sources"] >= max(1, int(min_sources))]
    rows.sort(key=lambda r: str(r["first_seen"]), reverse=True)          # ties: the newest first (stable sort)
    rows.sort(key=lambda r: (-r["momentum"], -len(r["sites"]), -r["sources"], -(r["score"] or 0)))
    return rows[:max(1, int(limit))]

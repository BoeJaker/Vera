"""Canvas engagement events (four-minds FM-6; owner yes 17 Oct, §13 Q7): the curator's outcomes.

The canvas curator (canvas.surface_rank, shadow) proposes which surfaces a turn should show; nothing so far said
whether the person then used what was shown. The canvas element reports, per item, four things a person does:

- opened   - opened it in place or maximised it;
- used     - acted on it (pinned it, ran it again, edited it, answered it, exploded it, read further);
- closed   - parked or removed it;
- restored - brought a parked or hidden item back.

Events are bounded (a batch of at most MAX_BATCH, short strings, the four kinds only), kept 30 days, and read per
surface by canvas.engagement.report: how often what was shown got opened or used versus closed.

Pure: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

EVENTS = ("opened", "used", "closed", "restored")
RETAIN_DAYS = 30
MAX_BATCH = 50
MAX_STR = 120


def _s(v: Any) -> str:
    return str(v or "").strip()[:MAX_STR]


def clean(events: Any, session_id: str = "") -> List[Dict[str, str]]:
    """The batch as stored: only the four kinds, a surface named, strings bounded, at most MAX_BATCH."""
    out: List[Dict[str, str]] = []
    for e in (events if isinstance(events, list) else [])[:MAX_BATCH]:
        if not isinstance(e, Mapping):
            continue
        ev, surface = _s(e.get("event")), _s(e.get("surface"))
        if ev not in EVENTS or not surface:
            continue
        out.append({"event": ev, "surface": surface, "kind": _s(e.get("kind")), "act": _s(e.get("act")),
                    "item": _s(e.get("key")), "canvas_id": _s(e.get("canvas")),
                    "session_id": _s(e.get("session") or session_id)})
    return out


def report(rows: Iterable[Mapping[str, Any]], min_events: int = 1) -> Dict[str, Any]:
    """Per surface: counts of each event, and engagement = (opened + used + restored) / all - the share of what
    a person did with it that was taking it up rather than putting it away."""
    by: Dict[str, Dict[str, int]] = {}
    for r in rows:
        s = str(r.get("surface") or "")
        ev = str(r.get("event") or "")
        if not s or ev not in EVENTS:
            continue
        c = by.setdefault(s, {k: 0 for k in EVENTS})
        c[ev] += int(r.get("n") or 1)
    surfaces = []
    for s, c in by.items():
        total = sum(c.values())
        if total < min_events:
            continue
        surfaces.append(dict(c, surface=s, total=total,
                             engagement=round((c["opened"] + c["used"] + c["restored"]) / total, 3)))
    surfaces.sort(key=lambda x: (-x["total"], x["surface"]))
    tot = {k: sum(c[k] for c in by.values()) for k in EVENTS}
    return {"surfaces": surfaces, "totals": tot, "events": sum(tot.values())}

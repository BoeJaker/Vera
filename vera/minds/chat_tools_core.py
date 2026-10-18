"""The chat tool shortlist in shadow (four-minds FM-3.2 / FM-3.3): which capabilities the page offered a chat turn,
which the server's caps.shortlist would have offered, and which the reply actually used - scored as recall@k.

The page picks up to 28 capabilities by word scoring (chat_panel _findRelevantCaps) and writes them into the message as
a manifest block; the model then uses one by writing [[cap:name ...]] (run) or [[suggest:cap name ...]] (offer) in its
reply. Both are text the server already holds, so the shadow needs no page change: it parses the page's list from the
message, the used capabilities from the reply, and compares the server's list (caps.shortlist on the user's words) on
the same turn. "Used" here is what the model chose to call, not whether the call succeeded.

Pure: no I/O.
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence

HINT_HEAD = "[INTEGRATED CAPABILITY MODE"
HINT_END = "\nFormats:"
KS = (16, 24, 28)
_MANIFEST_LINE = re.compile(r"^\s{2}([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\(", re.M)
_USED = re.compile(r"\[\[(?:cap:|suggest:cap\s+)([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)")


def offered(message: str) -> List[str]:
    """The page's manifest, in its order ([] when the message carries none)."""
    m = str(message or "")
    i = m.find(HINT_HEAD)
    if i < 0:
        return []
    j = m.find(HINT_END, i)
    block = m[i:j if j > 0 else len(m)]
    out: List[str] = []
    for name in _MANIFEST_LINE.findall(block):
        if name not in out:
            out.append(name)
    return out


def used(reply: str) -> List[str]:
    """Capabilities the reply asked to run or offered, first seen first."""
    out: List[str] = []
    for name in _USED.findall(str(reply or "")):
        if name not in out:
            out.append(name)
    return out


def row(session_id: str, query: str, client: Sequence[str], server: Sequence[str], used_caps: Sequence[str],
        ts: str = "") -> Dict[str, Any]:
    return {"ts": ts, "session": str(session_id or "")[:80], "query": str(query or "")[:200],
            "client": list(client)[:40], "server": list(server)[:40], "used": list(used_caps)[:20]}


def _position(name: str, lst: Sequence[str]) -> Optional[int]:
    try:
        return list(lst).index(name) + 1
    except ValueError:
        return None


def summarize(rows: Iterable[Dict[str, Any]], ks: Sequence[int] = KS) -> Dict[str, Any]:
    """Over turns that used at least one capability: recall@k of the used capabilities for the page's list and the
    server's list, the median position of a used capability in each, and how often the model used a capability the
    page never offered."""
    rows = list(rows)
    used_turns = [r for r in rows if r.get("used")]
    out: Dict[str, Any] = {"turns": len(rows), "turns_with_use": len(used_turns)}
    if not used_turns:
        return out
    for side in ("client", "server"):
        rec: Dict[str, Any] = {}
        for k in ks:
            hit = tot = 0
            for r in used_turns:
                top = list(r.get(side) or [])[:k]
                for u in r["used"]:
                    tot += 1
                    hit += 1 if u in top else 0
            rec[f"recall@{k}"] = round(hit / tot, 3) if tot else None
        pos = [p for r in used_turns for u in r["used"] for p in [_position(u, r.get(side) or [])] if p]
        rec["median_position"] = statistics.median(pos) if pos else None
        out[side] = rec
    unoffered = sum(1 for r in used_turns for u in r["used"] if u not in (r.get("client") or []))
    total_used = sum(len(r["used"]) for r in used_turns)
    out["unoffered_rate"] = round(unoffered / total_used, 3) if total_used else None
    return out

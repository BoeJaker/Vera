"""The board's flow (looplab-control §9.4 S8, 19 Oct 2026): units x lanes, what came in and what finished each day,
cycle time per unit, the age of open work, and time spent in each lane. Pure - board.flow feeds it the items and the
recorded lane moves (vera:board:moves, written by the board provider from 19 Oct on).

Before moves were recorded the board kept only created_at / updated_at, so a done item's finish time is its
updated_at - close, but a later comment or edit moves it. Every answer says which it used: basis "moves" or "approx".
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

DONE = "done"
CLOSED = ("done", "dropped")
NO_UNIT = "(no unit)"


def ts(v: Any) -> Optional[float]:
    """An ISO time (or epoch) as epoch seconds; None when unreadable."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def unit_of(item: Dict[str, Any]) -> str:
    """The unit an item belongs to: its unit:<name> label, else its plan's folder or name, else (no unit)."""
    for lab in item.get("labels") or []:
        if str(lab).startswith("unit:"):
            return str(lab)[5:] or NO_UNIT
    plan = str(item.get("plan") or "").strip().strip("/")
    if plan:
        parts = [p for p in plan.split("/") if p and p != "shared-planning"]
        name = parts[-2] if len(parts) >= 2 and "." in parts[-1] else (parts[-1] if parts else plan).rsplit(".", 1)[0]
        for pre in ("unit-", "plan-"):          # "unit-recon-fixes" is the unit:recon-fixes label's unit
            if name.startswith(pre) and len(name) > len(pre):
                return name[len(pre):]
        return name
    return NO_UNIT


def _q(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _h(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v / 3600.0, 1)


def matrix(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One cell per unit x lane: how many items are there."""
    by: Dict[tuple, int] = {}
    for it in items:
        k = (unit_of(it), str(it.get("lane") or "?"))
        by[k] = by.get(k, 0) + 1
    return [{"unit": u, "lane": l, "n": n} for (u, l), n in sorted(by.items())]


def done_at(item: Dict[str, Any], moves_by_item: Dict[str, List[Dict[str, Any]]]) -> tuple:
    """(when the item reached done, basis) - its last recorded move into done, else its updated_at."""
    ms = [m for m in moves_by_item.get(str(item.get("id")), []) if m.get("to") == DONE]
    if ms:
        return max(float(m["t"]) for m in ms), "moves"
    return ts(item.get("updated_at")), "approx"


def per_day(items: List[Dict[str, Any]], moves: List[Dict[str, Any]], since: float) -> List[Dict[str, Any]]:
    """Each day from `since`: items created, items finished, lane moves recorded."""
    mbi = by_item(moves)
    rows: Dict[str, Dict[str, Any]] = {}

    def row(d: str) -> Dict[str, Any]:
        return rows.setdefault(d, {"day": d, "created": 0, "done": 0, "moved": 0})

    for it in items:
        c = ts(it.get("created_at"))
        if c is not None and c >= since:
            row(day(c))["created"] += 1
        if it.get("lane") == DONE:
            t, _ = done_at(it, mbi)
            if t is not None and t >= since:
                row(day(t))["done"] += 1
    for m in moves:
        if float(m["t"]) >= since and m.get("frm"):
            row(day(float(m["t"])))["moved"] += 1
    return [rows[d] for d in sorted(rows)]


def by_item(moves: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for m in moves:
        if m.get("t") is None:
            continue
        out.setdefault(str(m.get("id")), []).append(m)
    for v in out.values():
        v.sort(key=lambda m: float(m["t"]))
    return out


def cycle(items: List[Dict[str, Any]], moves: List[Dict[str, Any]], since: float) -> List[Dict[str, Any]]:
    """Per unit, items finished since `since`: created -> done, p50 / p90 in hours, and which basis each used."""
    mbi = by_item(moves)
    per: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if it.get("lane") != DONE:
            continue
        t, basis = done_at(it, mbi)
        c = ts(it.get("created_at"))
        if t is None or c is None or t < since or t < c:
            continue
        u = per.setdefault(unit_of(it), {"spans": [], "moves": 0, "approx": 0})
        u["spans"].append(t - c)
        u[basis] += 1
    out = []
    for name, u in per.items():
        out.append({"unit": name, "done": len(u["spans"]), "p50_h": _h(_q(u["spans"], 0.5)),
                    "p90_h": _h(_q(u["spans"], 0.9)),
                    "basis": "moves" if not u["approx"] else ("approx" if not u["moves"] else "mixed")})
    return sorted(out, key=lambda r: (-r["done"], r["unit"]))


def wip(items: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """Per unit, the open items (not done or dropped): how many, the oldest and the median age in hours."""
    per: Dict[str, List[float]] = {}
    for it in items:
        if it.get("lane") in CLOSED:
            continue
        c = ts(it.get("created_at"))
        per.setdefault(unit_of(it), []).append(max(0.0, now - c) if c is not None else 0.0)
    return sorted(({"unit": u, "open": len(a), "oldest_h": _h(max(a)), "median_h": _h(_q(a, 0.5))}
                   for u, a in per.items()), key=lambda r: (-(r["oldest_h"] or 0), r["unit"]))


def lane_time(moves: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """Time spent in each lane, from consecutive recorded moves of one item (the lane it is in now runs to `now`)."""
    spent: Dict[str, List[float]] = {}
    for seq in by_item(moves).values():
        for a, b in zip(seq, seq[1:] + [None]):
            lane = a.get("to")
            if not lane or lane in CLOSED:
                continue
            end = float(b["t"]) if b else now
            spent.setdefault(lane, []).append(max(0.0, end - float(a["t"])))
    return sorted(({"lane": l, "n": len(v), "p50_h": _h(_q(v, 0.5)), "p90_h": _h(_q(v, 0.9))}
                   for l, v in spent.items()), key=lambda r: r["lane"])


def parse_moves(entries: Iterable[Any]) -> List[Dict[str, Any]]:
    """Stream entries (id, {field: value}) -> [{id, frm, to, by, t, at}]; unreadable ones are skipped."""
    out = []
    for _sid, f in entries or []:
        g = {(k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
             for k, v in (f or {}).items()}
        t = ts(g.get("at"))
        if t is None or not g.get("id"):
            continue
        out.append({"id": g["id"], "frm": g.get("frm", ""), "to": g.get("to", ""), "by": g.get("by", ""),
                    "t": t, "at": g.get("at")})
    return out


def flow(items: List[Dict[str, Any]], moves: List[Dict[str, Any]], now: float, days: int) -> Dict[str, Any]:
    since = now - max(1, int(days)) * 86400
    mv = [m for m in moves if float(m["t"]) >= since]
    first = min((float(m["t"]) for m in moves), default=None)
    return {"cells": matrix(items), "days": per_day(items, moves, since), "cycle": cycle(items, moves, since),
            "wip": wip(items, now), "lane_time": lane_time(moves, now),
            "moves": [dict(m, title="") for m in mv[-400:]],
            "moves_recorded": len(moves), "moves_since": None if first is None else day(first),
            "units": sorted({unit_of(i) for i in items})}
